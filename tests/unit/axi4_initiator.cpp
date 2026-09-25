#include "core/axi4_initiator.hpp"
#include "core/ir_cone.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <set>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
Axi4InitiatorProfile profile(unsigned data_bits = 32, unsigned len_bits = 8, unsigned id_bits = 4,
                             bool read = true, bool write = true) {
  Axi4InitiatorProfile p;
  p.name = "probe";
  p.data_bits = data_bits;
  p.len_bits = len_bits;
  p.id_bits = id_bits;
  p.read = read;
  p.write = write;
  return p;
}
bool has_diagnostic(const std::vector<Diagnostic> &diagnostics, const std::string &code) {
  return std::ranges::any_of(diagnostics, [&](const auto &d) { return d.code == code; });
}
const Port *find_port(const HardwareIR &ir, const std::string &name) {
  auto it = std::ranges::find_if(ir.ports, [&](const auto &p) { return p.name == name; });
  return it != ir.ports.end() ? &*it : nullptr;
}
void bound_states_zeroed(const NodeHardware &h) {
  std::set<std::size_t> actual;
  for (const auto &[name, id] : h.bindings)
    if (h.ir.values.at(id.index).op == Op::State)
      actual.insert(id.index);
  REQUIRE(actual.size() == h.ir.states.size());
  for (const auto &s : h.ir.states) {
    REQUIRE(actual.contains(s.output.index));
    REQUIRE(s.reset.has_value());
    REQUIRE(s.reset->value == 0);
  }
}
} // namespace

TEST_CASE("AXI4 initiator accepts the parameterized profile matrix", "[axi4-initiator]") {
  for (unsigned data_bits : {32u, 64u, 128u, 256u, 512u, 1024u})
    for (unsigned len_bits : {4u, 8u})
      for (unsigned id_bits : {0u, 1u, 8u})
        for (bool read_only : {false, true}) {
          auto p = profile(data_bits, len_bits, id_bits, true, !read_only);
          CAPTURE(data_bits, len_bits, id_bits, read_only);
          auto lowered = lower_axi4_initiator(p);
          REQUIRE(lowered);
          const auto &h = lowered.value();
          REQUIRE(verify(h.ir));
          REQUIRE(h.ir.name == "nocloom_axi4_initiator_probe");
          bound_states_zeroed(h);
          // Deterministic lowering: repeated generation is byte-identical.
          auto second = lower_axi4_initiator(p);
          REQUIRE(second);
          REQUIRE(emit_verilog(h.ir) == emit_verilog(second.value().ir));
        }
}

TEST_CASE("AXI4 initiator signal face follows the contract field order", "[axi4-initiator]") {
  auto lowered = lower_axi4_initiator(profile(64, 8, 4));
  REQUIRE(lowered);
  const auto &ir = lowered.value().ir;
  // AXI4 slave face, full signal set minus QoS/USER/REGION.
  for (const auto *name :
       {"nl_axi_awid",    "nl_axi_awaddr",  "nl_axi_awlen",   "nl_axi_awsize",  "nl_axi_awburst",
        "nl_axi_awlock",  "nl_axi_awcache", "nl_axi_awprot",  "nl_axi_awvalid", "nl_axi_awready",
        "nl_axi_wdata",   "nl_axi_wstrb",   "nl_axi_wlast",   "nl_axi_wvalid",  "nl_axi_wready",
        "nl_axi_bid",     "nl_axi_bresp",   "nl_axi_bvalid",  "nl_axi_bready",  "nl_axi_arid",
        "nl_axi_araddr",  "nl_axi_arlen",   "nl_axi_arsize",  "nl_axi_arburst", "nl_axi_arlock",
        "nl_axi_arcache", "nl_axi_arprot",  "nl_axi_arvalid", "nl_axi_arready", "nl_axi_rid",
        "nl_axi_rdata",   "nl_axi_rresp",   "nl_axi_rlast",   "nl_axi_rvalid",  "nl_axi_rready"})
    REQUIRE(find_port(ir, name) != nullptr);
  for (const auto *name :
       {"nl_axi_awqos", "nl_axi_awuser", "nl_axi_awregion", "nl_axi_wuser", "nl_axi_buser",
        "nl_axi_arqos", "nl_axi_aruser", "nl_axi_arregion", "nl_axi_ruser"})
    REQUIRE(find_port(ir, name) == nullptr);
  REQUIRE(find_port(ir, "nl_axi_awlen")->width == 8);
  REQUIRE(find_port(ir, "nl_axi_awid")->width == 4);
  REQUIRE(find_port(ir, "nl_axi_wdata")->width == 64);
  REQUIRE(find_port(ir, "nl_axi_wstrb")->width == 8);
  // Weft faces carry the contract fields in travel order.
  const BurstProfile burst{4, 8};
  const auto request = burst_request_fields(64, burst, 0);
  const auto response = burst_response_fields(64, burst, 0);
  std::vector<std::string> wreq_ports, wrsp_ports, rreq_ports, rrsp_ports;
  for (const auto &port : ir.ports) {
    if (port.endpoint == "wreq" && port.signal != "ready" && port.signal != "valid")
      wreq_ports.push_back(port.signal);
    if (port.endpoint == "wrsp" && port.signal != "valid" && port.signal != "ready")
      wrsp_ports.push_back(port.signal);
    if (port.endpoint == "rreq" && port.signal != "ready" && port.signal != "valid")
      rreq_ports.push_back(port.signal);
    if (port.endpoint == "rrsp" && port.signal != "valid" && port.signal != "ready")
      rrsp_ports.push_back(port.signal);
  }
  std::vector<std::string> request_names, response_names;
  for (const auto &[field, width] : request)
    request_names.push_back(field);
  for (const auto &[field, width] : response)
    response_names.push_back(field);
  REQUIRE(wreq_ports == request_names);
  REQUIRE(rreq_ports == request_names);
  REQUIRE(wrsp_ports == response_names);
  REQUIRE(rrsp_ports == response_names);
  // Port widths follow the contract widths.
  for (const auto &[field, width] : request) {
    REQUIRE(find_port(ir, "nl_wreq_" + field)->width == width);
    REQUIRE(find_port(ir, "nl_rreq_" + field)->width == width);
  }
  for (const auto &[field, width] : response) {
    REQUIRE(find_port(ir, "nl_wrsp_" + field)->width == width);
    REQUIRE(find_port(ir, "nl_rrsp_" + field)->width == width);
  }
  // Internal observation bindings for the bench and the designated faults.
  for (const auto *key : {"wr.state",          "wr.remaining",      "wr.beat_addr", "wr.poisoned",
                          "wr.admitted",       "wr.span_cross",     "wr.budget",    "wr.span",
                          "wr.frag_first",     "wr.frag_last",      "wr.accept",    "wr.transfer",
                          "wr.wlast_expected", "wr.wlast_mismatch", "wr.addr_incr", "wr.addr_next",
                          "wr.addr_next_mode", "wr.wrap_turn",      "wr.wrap_base", "wr.wrap_mask",
                          "wr.exclusive_ok",   "wr.size_ok",        "wr.burst_ok",  "wr.wrap_ok",
                          "wr.fixed_ok",       "wr.incr_ok",        "wr.aw_fire",   "wr.wrsp_fire",
                          "wr.beat_step",      "rr.state",          "rr.remaining", "rr.admitted",
                          "rr.accept",         "rr.rsp_full",       "rr.rlast",     "rr.ar_fire",
                          "rr.emit_fire",      "rr.rsp_load",       "wr.refill"})
    REQUIRE(lowered.value().bindings.contains(key));
}

TEST_CASE("AXI4 initiator omits the ID face and the write face per profile", "[axi4-initiator]") {
  {
    auto lowered = lower_axi4_initiator(profile(32, 4, 0));
    REQUIRE(lowered);
    const auto &ir = lowered.value().ir;
    for (const auto *name : {"nl_axi_awid", "nl_axi_bid", "nl_axi_arid", "nl_axi_rid", "nl_wreq_id",
                             "nl_wrsp_id", "nl_rreq_id", "nl_rrsp_id"})
      REQUIRE(find_port(ir, name) == nullptr);
    REQUIRE(find_port(ir, "nl_axi_awlen")->width == 4);
    REQUIRE(find_port(ir, "nl_axi_wdata")->width == 32);
    REQUIRE(find_port(ir, "nl_axi_wstrb")->width == 4);
  }
  {
    auto lowered = lower_axi4_initiator(profile(32, 8, 4, true, false));
    REQUIRE(lowered);
    const auto &ir = lowered.value().ir;
    for (const auto *name : {"nl_axi_awvalid", "nl_axi_wvalid", "nl_axi_bvalid", "nl_axi_awready",
                             "nl_axi_wready", "nl_axi_bresp"})
      REQUIRE(find_port(ir, name) == nullptr);
    for (const auto &port : ir.ports) {
      REQUIRE(port.endpoint != "wreq");
      REQUIRE(port.endpoint != "wrsp");
    }
    REQUIRE(find_port(ir, "nl_axi_arvalid") != nullptr);
    REQUIRE(find_port(ir, "nl_rreq_valid") != nullptr);
  }
}

TEST_CASE("AXI4 initiator rejects out-of-domain parameters", "[axi4-initiator]") {
  for (unsigned data_bits : {8u, 16u, 2048u, 24u}) {
    auto result = lower_axi4_initiator(profile(data_bits));
    CAPTURE(data_bits);
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_DATA_WIDTH"));
  }
  for (unsigned len_bits : {0u, 3u, 9u, 16u}) {
    auto result = lower_axi4_initiator(profile(32, len_bits));
    CAPTURE(len_bits);
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_LEN_WIDTH"));
  }
  for (unsigned id_bits : {17u, 32u}) {
    auto result = lower_axi4_initiator(profile(32, 8, id_bits));
    CAPTURE(id_bits);
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_ID_WIDTH"));
  }
  for (unsigned address_bits : {0u, 49u}) {
    auto p = profile();
    p.address_bits = address_bits;
    auto result = lower_axi4_initiator(p);
    CAPTURE(address_bits);
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_ADDRESS_WIDTH"));
  }
  {
    // Write-only is rejected: only read-write and
    // read-only sockets are provided.
    auto result = lower_axi4_initiator(profile(32, 8, 4, false, true));
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_DIRECTION"));
  }
  {
    auto p = profile();
    p.name = "has space";
    auto result = lower_axi4_initiator(p);
    REQUIRE(!result);
    REQUIRE(has_diagnostic(result.diagnostics(), "NCL_AXI4_INITIATOR_NAME"));
  }
}

TEST_CASE("Burst packet contract field families", "[axi4-initiator]") {
  {
    const std::vector<std::pair<std::string, unsigned>> expected{
        {"address", 32}, {"write", 1}, {"protection", 3}, {"length", 8},
        {"size", 3},     {"burst", 2}, {"cache", 4},      {"id", 4},
        {"count", 6},    {"ends", 1},  {"wrap_end", 1},   {"error", 1},
        {"first", 1},    {"last", 1},  {"data", 32},      {"be", 4}};
    REQUIRE(burst_request_fields(32, {4, 8}, 0) == expected);
  }
  {
    const std::vector<std::pair<std::string, unsigned>> expected{
        {"address", 32}, {"result", 2}, {"id", 8},   {"source", 2}, {"count", 6},
        {"error", 1},    {"first", 1},  {"last", 1}, {"data", 64}};
    REQUIRE(burst_response_fields(64, {8, 4}, 2) == expected);
  }
  {
    // No id field and no source field when the widths are zero.
    const auto request = burst_request_fields(32, {0, 4}, 0);
    REQUIRE(request.size() == 15);
    REQUIRE(std::ranges::none_of(request, [](const auto &f) { return f.first == "id"; }));
    REQUIRE(std::ranges::none_of(request, [](const auto &f) { return f.first == "source"; }));
    const auto response = burst_response_fields(32, {0, 4}, 0);
    REQUIRE(response.size() == 7);
    REQUIRE(std::ranges::none_of(response, [](const auto &f) { return f.first == "id"; }));
  }
}

TEST_CASE("Weft word messages have their own layout beside the burst profile", "[axi4-initiator]") {
  // The burst packet contract is a sibling field family; describe_messages does
  // not depend on it.
  const auto result = describe_messages({32, 32, 16});
  REQUIRE(result);
  const std::vector<MessageField> request{
      {MessageFieldKind::Address, 32},   {MessageFieldKind::Write, 1},
      {MessageFieldKind::WriteData, 32}, {MessageFieldKind::ByteEnable, 4},
      {MessageFieldKind::Protection, 3}, {MessageFieldKind::Source, 4}};
  REQUIRE(result.value().request == request);
}

TEST_CASE("AXI4 ingress extends narrow ports into the declared transport", "[axi4-initiator]") {
  auto p = profile();
  p.address_bits = 17;
  p.transport_address_bits = 40;
  auto h = lower_axi4_initiator(p);
  REQUIRE(h);
  REQUIRE(find_port(h.value().ir, "nl_axi_awaddr"));
  CHECK(find_port(h.value().ir, "nl_axi_awaddr")->width == 17);
  REQUIRE(find_port(h.value().ir, "nl_wreq_address"));
  CHECK(find_port(h.value().ir, "nl_wreq_address")->width == 40);
  p.address_bits = 11;
  REQUIRE_FALSE(lower_axi4_initiator(p));
  p.address_bits = 17;
  p.transport_address_bits = 31;
  REQUIRE_FALSE(lower_axi4_initiator(p));
  p.transport_address_bits = 48;
  REQUIRE(lower_axi4_initiator(p));
  p.transport_address_bits = 49;
  REQUIRE_FALSE(lower_axi4_initiator(p));
}

TEST_CASE("Wide full AXI ingress does not widen Lite", "[axi4-initiator]") {
  auto p = profile(128);
  p.lite = true;
  REQUIRE_FALSE(lower_axi4_initiator(p));
}

TEST_CASE("Narrow APB faces retain a legal internal engine", "[axi4-initiator]") {
  for (auto protocol : {Protocol::Apb3, Protocol::Apb4})
    for (unsigned bits : {8U, 16U, 32U}) {
      auto p = profile(32, 8, 0);
      p.lite = true;
      p.apb = protocol;
      p.apb_data_bits = bits;
      auto lowered = lower_axi4_initiator(p);
      REQUIRE(lowered);
      const auto &h = lowered.value();
      REQUIRE(verify(h.ir));
      CHECK(find_port(h.ir, "nl_apb_pwdata")->width == bits);
      CHECK(find_port(h.ir, "nl_apb_prdata")->width == bits);
      CHECK(find_port(h.ir, "nl_wreq_data")->width == 32);
      CHECK(find_port(h.ir, "nl_rrsp_data")->width == 32);
      if (protocol == Protocol::Apb4)
        CHECK(find_port(h.ir, "nl_apb_pstrb")->width == bits / 8);
      else
        CHECK(find_port(h.ir, "nl_apb_pstrb") == nullptr);
      bound_states_zeroed(h);
      p.address_bits = 1;
      auto small_address = lower_axi4_initiator(p);
      REQUIRE(small_address);
      CHECK(verify(small_address.value().ir));
      p.address_bits = 32;
      for (unsigned invalid : {0U, 24U, 64U}) {
        p.apb_data_bits = invalid;
        CHECK_FALSE(lower_axi4_initiator(p));
      }
      p.apb_data_bits = bits;
      p.max_outstanding = 2;
      CHECK_FALSE(lower_axi4_initiator(p));
      p.max_outstanding = 1;
      p.read = false;
      CHECK_FALSE(lower_axi4_initiator(p));
    }
}

TEST_CASE("APB read extraction uses the accepted source word", "[axi4-initiator]") {
  for (unsigned bits : {8U, 16U}) {
    auto p = profile(32, 8, 0);
    p.lite = true;
    p.apb = Protocol::Apb4;
    p.apb_data_bits = bits;
    auto lowered = lower_axi4_initiator(p);
    REQUIRE(lowered);
    const auto &h = lowered.value();
    std::map<std::size_t, std::uint64_t> states, inputs;
    auto set = [&](const char *name, std::uint64_t value) {
      states[h.ir.values.at(h.bindings.at(name).index).state.index] = value;
    };
    for (std::size_t i = 0; i < h.ir.ports.size(); ++i)
      if (h.ir.ports[i].name == "nl_reset_n")
        inputs[i] = 1;
    const auto output = std::ranges::find_if(h.ir.drivers, [&](const auto &driver) {
      return h.ir.ports.at(driver.port.index).name == "nl_apb_prdata";
    });
    REQUIRE(output != h.ir.drivers.end());
    const auto paddr = [&](std::uint64_t value) {
      for (std::size_t i = 0; i < h.ir.ports.size(); ++i)
        if (h.ir.ports[i].name == "nl_apb_paddr")
          inputs[i] = value;
    };
    set("rr.rsp_data", 0xd3a75c19);
    for (unsigned lane = 0; lane < 4; lane += bits / 8) {
      const auto want = (0xd3a75c19U >> (lane * 8)) & ((1U << bits) - 1);
      // The answer: the manager still holds its PADDR.
      set("rr.state", 2);
      set("rr.rsp_full", 1);
      paddr(0x1000 + lane);
      CHECK(evaluate_cone(h.ir, output->value.index, states, inputs) == want);
      set("rr.rsp_error", 1);
      CHECK(evaluate_cone(h.ir, output->value.index, states, inputs) == 0);
      set("rr.rsp_error", 0);
      // Between answers the lane stays whatever the next PADDR says.
      set("rr.state", 0);
      set("rr.rsp_full", 0);
      set("rr.prdata_read", 1);
      set("rr.prdata_lane", lane);
      for (unsigned following = 0; following < 4; ++following) {
        paddr(0x2000 + following);
        CHECK(evaluate_cone(h.ir, output->value.index, states, inputs) == want);
      }
    }
  }
}

TEST_CASE("Explicit transport extends single-transfer source addresses", "[axi4-initiator]") {
  for (unsigned data : {8U, 16U, 32U, 64U}) {
    auto p = profile(data == 64 ? 64 : 32, 8, 0);
    p.lite = true;
    if (data != 64) {
      p.apb = Protocol::Apb4;
      p.apb_data_bits = data;
    }
    p.transport_address_bits = 40;
    const unsigned minimum = p.apb ? 1U : 2U;
    for (unsigned bits : {minimum, 11U}) {
      p.address_bits = bits;
      auto h = lower_axi4_initiator(p);
      REQUIRE(h);
      CHECK(verify(h.value().ir));
      CHECK(find_port(h.value().ir, p.apb ? "nl_apb_paddr" : "nl_axi_awaddr")->width == bits);
      CHECK(find_port(h.value().ir, "nl_wreq_address")->width == 40);
    }
    p.address_bits = minimum - 1;
    CHECK_FALSE(lower_axi4_initiator(p));
  }
}

TEST_CASE("Single-transfer admission windows preserve whole source words", "[axi4-initiator]") {
  auto p = profile(64, 8, 0);
  p.lite = true;
  p.admitted_windows = std::vector<AddressWindow>{{0x1100, 0x90}};
  REQUIRE(lower_axi4_initiator(p));
  p.admitted_windows->front().size = 4;
  CHECK_FALSE(lower_axi4_initiator(p));
  p.admitted_windows->front() = {0x1104, 8};
  CHECK_FALSE(lower_axi4_initiator(p));
  p = profile(32, 8, 0);
  p.lite = true;
  p.apb = Protocol::Apb4;
  p.apb_data_bits = 8;
  p.admitted_windows = std::vector<AddressWindow>{{3, 1}};
  REQUIRE(lower_axi4_initiator(p));
  p.apb.reset();
  p.lite = false;
  p.admitted_windows->front() = {0x1100, 0x90};
  CHECK_FALSE(lower_axi4_initiator(p));
}

TEST_CASE("AXI4 initiator preserves extended ID fields", "[axi4-initiator]") {
  for (unsigned bits : {9U, 16U}) {
    auto lowered = lower_axi4_initiator(profile(32, 8, bits));
    REQUIRE(lowered);
    REQUIRE(verify(lowered.value().ir));
    for (const auto &port : lowered.value().ir.ports)
      if (port.name.ends_with("awid") || port.name.ends_with("arid") ||
          port.name.ends_with("bid") || port.name.ends_with("rid"))
        CHECK(port.width == bits);
  }
}

// A locked INCR fails whole when the packet splitter would cut it, and the splitter cuts by the
// first of its windows holding the start (the most specific mapping): a window holding it later
// (an outer mapping) never decides, and a window the ingress does not check never fails it.
TEST_CASE("A locked burst is checked against the first splitter window holding its start",
          "[axi4-initiator][lock-windows]") {
  auto p = profile(32, 8, 2);
  p.lock_windows = {{{0x1000, 0x1000}, 4096, 4096, 4},
                    {{0x3000, 0x1000}, 4096, 4096, 4, false},
                    {{0x0, 0x10000}, 128, 256, 4}};
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  const auto &n = lowered.value();
  auto state = [&](const std::string &key) {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  };
  auto lock_ok = [&](std::uint64_t addr, std::uint64_t len, std::uint64_t burst = 1,
                     std::uint64_t lock = 1) {
    return evaluate_cone(n.ir, n.bindings.at("rr.lock_ok").index,
                         {{state("rr.state"), 1},
                          {state("rr.desc.addr"), addr},
                          {state("rr.desc.len"), len},
                          {state("rr.desc.size"), 2},
                          {state("rr.desc.burst"), burst},
                          {state("rr.desc.lock"), lock}}) == 1U;
  };
  CHECK(lock_ok(0x1000, 63));       // 256 bytes inside the inner window's limits
  CHECK(lock_ok(0x10f0, 7));        // across the outer window's 256-byte boundary
  CHECK(lock_ok(0x3000, 63));       // a window not checked
  CHECK_FALSE(lock_ok(0x5000, 63)); // the outer window: over its 128-byte packet limit
  CHECK(lock_ok(0x5000, 31));       // 128 bytes from the aligned start
  CHECK_FALSE(lock_ok(0x50f0, 7));  // across its 256-byte boundary
  CHECK(lock_ok(0x5000, 63, 1, 0)); // not locked
  CHECK(lock_ok(0x5000, 63, 0));    // FIXED is never cut
  CHECK(lock_ok(0x20000, 255));     // outside every window
}
