#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <regex>
#include <string>

namespace {
// An ACE5-Lite initiator and target beside an AXI4 initiator and an APB4 target. PORT and OBJECT
// set the protocol and the axi5 object of the two ACE5-Lite endpoints.
const char *const yaml = R"(schema_version: 10
name: ace5_ports
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: PORT, data_width: 64, id_width: 3, len_width: 8,
     axi5: OBJECT}
  - {name: dma, clock_domain: main, protocol: axi4, data_width: 32}
targets:
  - {name: ram, clock_domain: main, protocol: PORT, data_width: 32, len_width: 8,
     attributes: amba, axi5: OBJECT, mappings: [{base: '0x0', size: '0x10000'}]}
  - {name: uart, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x10000', size: '0x1000'}]}
transport: {data_width: 32}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
std::string with(const std::string &protocol, const std::string &object = "{}") {
  return std::regex_replace(std::regex_replace(yaml, std::regex("PORT"), protocol),
                            std::regex("OBJECT"), object);
}
nocloom::Definition parsed(const std::string &text) {
  const auto result = nocloom::config::parse(text, "ace5_ports.yaml");
  REQUIRE(result);
  return result.value().definition;
}
const std::string &artifact(const nocloom::ArtifactSet &set, const std::string &suffix) {
  const auto at =
      std::ranges::find_if(set, [&](const auto &a) { return a.path.ends_with(suffix); });
  REQUIRE(at != set.end());
  return at->text;
}
std::vector<nocloom::Diagnostic> refused(const nocloom::Definition &d) {
  const auto r = nocloom::generate(d);
  REQUIRE_FALSE(r);
  return r.diagnostics();
}
bool has(const std::vector<nocloom::Diagnostic> &list, const std::string &code,
         const std::string &field, const std::string &text = "") {
  return std::ranges::any_of(list, [&](const nocloom::Diagnostic &x) {
    return x.code == code && x.field == field && x.message.find(text) != std::string::npos;
  });
}
bool port(const std::string &rtl, const std::string &declaration) {
  return rtl.find(declaration) != std::string::npos;
}
} // namespace

TEST_CASE("ACE5-Lite endpoints have the shareable transaction ports", "[ace5-lite]") {
  using namespace nocloom;
  const auto ace = parsed(with("ace5_lite"));
  CHECK(ace.initiators[0].protocol == Protocol::Ace5Lite);
  GenerationOptions options;
  options.ipxact = true;
  const auto r = generate(ace, options);
  REQUIRE(r);
  const auto &rtl = artifact(r.value(), ".v");
  for (const auto *declaration :
       {"input wire [1:0] nl_i_cpu_awdomain,", "input wire [3:0] nl_i_cpu_awsnoop,",
        "input wire [1:0] nl_i_cpu_ardomain,", "input wire [3:0] nl_i_cpu_arsnoop,",
        "output wire [1:0] nl_t_ram_awdomain,", "output wire [3:0] nl_t_ram_awsnoop,",
        "output wire [1:0] nl_t_ram_ardomain,", "output wire [3:0] nl_t_ram_arsnoop,",
        "nl_i_cpu_awprot,", "nl_t_ram_awprot,"})
    CHECK(port(rtl, declaration));
  // Issue L has no barriers, and the AXI4 initiator keeps its ports.
  for (const auto *absent : {"awbar", "arbar", "nl_i_dma_awdomain", "nl_i_dma_awsnoop"})
    CHECK_FALSE(port(rtl, absent));
  const auto &map = artifact(r.value(), ".map.json");
  CHECK(port(map, R"("cpu": "ace5_lite")"));
  CHECK(port(map, R"("ram": "ace5_lite")"));
  const auto &xml = artifact(r.value(), ".xml");
  CHECK(port(xml, R"(spirit:library="AMBA5" spirit:name="ACE5-Lite" spirit:version="r0p0_0")"));
  CHECK(port(xml, "<spirit:name>AWDOMAIN</spirit:name>"));
  // The same properties on axi5 endpoints give the same hardware.
  const auto axi5 =
      parsed(with("axi5", "{shareable: true, write_snoop_width: 4, read_snoop_width: 4}"));
  const auto same = generate(axi5, options);
  REQUIRE(same);
  CHECK(artifact(same.value(), ".v") == rtl);
  // A target port may omit AxSNOOP: it then sends only opcodes of zero.
  auto target_only = parsed(with("ace5_lite"));
  target_only.targets[0].axi5.emplace().write_snoop_width = 0;
  target_only.targets[0].axi5->read_snoop_width = 0;
  const auto narrow = generate(target_only);
  REQUIRE(narrow);
  const auto &narrow_rtl = artifact(narrow.value(), ".v");
  CHECK(port(narrow_rtl, "output wire [1:0] nl_t_ram_awdomain,"));
  CHECK_FALSE(port(narrow_rtl, "nl_t_ram_awsnoop"));
  CHECK_FALSE(port(narrow_rtl, "nl_t_ram_arsnoop"));
  // An axi5 endpoint with only shareable: true has the domain ports.
  const auto domain = generate(parsed(with("axi5", "{shareable: true}")));
  REQUIRE(domain);
  const auto &domain_rtl = artifact(domain.value(), ".v");
  CHECK(port(domain_rtl, "input wire [1:0] nl_i_cpu_ardomain,"));
  CHECK(port(domain_rtl, "output wire [1:0] nl_t_ram_awdomain,"));
  CHECK_FALSE(port(domain_rtl, "snoop"));
}

TEST_CASE("An ACE5-Lite target ID port has at most 31 bits", "[ace5-lite]") {
  using namespace nocloom;
  auto d = parsed(with("ace5_lite"));
  d.targets[0].id_width = 31;
  CHECK(generate(d));
  d.targets[0].id_width = 32;
  CHECK(has(refused(d), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[0].id_width",
            "ACE5-Lite target ID ports have 0 to 31 bits"));
  d = parsed(with("ace5_lite"));
  d.targets[0].len_width = 9;
  CHECK(has(refused(d), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[0].len_width",
            "ACE5-Lite target length ports have 4 to 8 bits"));
}

TEST_CASE("ACE5-Lite ports take part in exclusive access", "[ace5-lite]") {
  using namespace nocloom;
  // The ACE5-Lite target has AxLOCK: exclusive: target only asks for owner flags.
  auto d = parsed(with("ace5_lite"));
  d.targets[0].exclusive = ExclusiveAccess::Target;
  auto list = refused(d);
  CHECK(has(list, "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive_owner_flags"));
  CHECK_FALSE(has(list, "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive"));
  d = parsed(with("ace5_lite"));
  d.targets[1].exclusive = ExclusiveAccess::Target;
  CHECK(has(refused(d), "NCL_TARGET_EXCLUSIVE", "targets[1].exclusive",
            "Only AXI5, ACE5-Lite, AXI4 and AXI3 targets have AxLOCK"));
  // The read-write ACE5-Lite initiator alone can send exclusive accesses.
  d = parsed(with("ace5_lite"));
  d.targets[0].exclusive = ExclusiveAccess::Network;
  d.initiators[1].access = Access::ReadOnly;
  CHECK(generate(d));
  d.initiators[0].access = Access::ReadOnly;
  CHECK(has(refused(d), "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive",
            "No read-write AXI5, ACE5-Lite, AXI4 or AXI3 initiator"));
}

TEST_CASE("ACE5-Lite constraints and refused coherency properties", "[ace5-lite]") {
  using namespace nocloom;
  const auto base = parsed(with("ace5_lite"));
  const auto edit = [&](bool initiator, const std::function<void(Axi5Properties &)> &change) {
    auto d = base;
    auto &p = (initiator ? d.initiators[0].axi5 : d.targets[0].axi5).emplace();
    change(p);
    return refused(d);
  };
  const std::string source = "initiators[0].axi5.", target = "targets[0].axi5.";
  // IHI 0022 Issue L, B2.1.2 and table B2.4: the ACE5-Lite column.
  CHECK(has(edit(true, [](auto &p) { p.shareable = false; }), "NCL_AXI5_PROPERTIES",
            source + "shareable"));
  CHECK(has(edit(false, [](auto &p) { p.shareable = false; }), "NCL_AXI5_PROPERTIES",
            target + "shareable"));
  CHECK(has(edit(true,
                 [](auto &p) {
                   p.prot = false;
                   p.priv_inst = true;
                 }),
            "NCL_AXI5_PROPERTIES", source + "priv_inst"));
  CHECK(has(edit(false,
                 [](auto &p) {
                   p.prot = false;
                   p.pas_width = 1;
                 }),
            "NCL_AXI5_PROPERTIES", target + "pas_width"));
  // Table B2.2: AxPROT at a Manager interface, AxSNOOP at a Subordinate interface.
  CHECK(has(edit(false, [](auto &p) { p.prot = false; }), "NCL_AXI5_PROPERTIES", target + "prot"));
  CHECK(has(edit(true, [](auto &p) { p.write_snoop_width = 0; }), "NCL_AXI5_PROPERTIES",
            source + "write_snoop_width"));
  CHECK(has(edit(true, [](auto &p) { p.read_snoop_width = 0; }), "NCL_AXI5_PROPERTIES",
            source + "read_snoop_width"));
  {
    auto d = base;
    d.initiators[0].axi5.emplace().prot = false;
    CHECK(generate(d));
  }
  // DVM and cache maintenance have diagnostics on both protocols.
  for (const bool ace : {true, false}) {
    auto start = ace ? base : parsed(with("axi5", "{shareable: true}"));
    const auto refuse = [&](const std::function<void(Axi5Properties &)> &change,
                            const std::string &field, const std::string &property) {
      auto d = start;
      auto &p = d.initiators[0].axi5.emplace(d.initiators[0].axi5.value_or(Axi5Properties{}));
      change(p);
      INFO(field);
      CHECK(has(refused(d), "NCL_AXI5_UNSUPPORTED", source + field, property));
    };
    refuse([](auto &p) { p.dvm = Axi5Dvm::Receiver; }, "dvm", "DVM_Message_Support");
    refuse([](auto &p) { p.coherency_connection = true; }, "coherency_connection",
           "Coherency_Connection_Signals");
    refuse([](auto &p) { p.cmo_on_read = true; }, "cmo_on_read", "CMO_On_Read");
    refuse([](auto &p) { p.cmo_on_write = true; }, "cmo_on_write", "CMO_On_Write");
    refuse([](auto &p) { p.write_plus_cmo = true; }, "write_plus_cmo", "Write_Plus_CMO");
    refuse([](auto &p) { p.persist_cmo = true; }, "persist_cmo", "Persist_CMO");
    refuse([](auto &p) { p.storage_cmo = true; }, "storage_cmo", "Storage_CMO");
    refuse([](auto &p) { p.cache_stash = Axi5CacheStash::Basic; }, "cache_stash",
           "Cache_Stash_Transactions");
    refuse([](auto &p) { p.prefetch = true; }, "prefetch", "Prefetch_Transaction");
    refuse([](auto &p) { p.write_zero = true; }, "write_zero", "WriteZero_Transaction");
    refuse([](auto &p) { p.write_no_snoop_full = true; }, "write_no_snoop_full",
           "WriteNoSnoopFull_Transaction");
    refuse([](auto &p) { p.invalidate_hint = true; }, "invalidate_hint",
           "InvalidateHint_Transaction");
    refuse([](auto &p) { p.deallocation = true; }, "deallocation", "DeAllocation_Transactions");
    refuse([](auto &p) { p.shareable_cache = true; }, "shareable_cache", "Shareable_Cache_Support");
    refuse([](auto &p) { p.write_snoop_width = 5; }, "write_snoop_width", "AWSNOOP_WIDTH");
    refuse([](auto &p) { p.cache_line_bytes = 128; }, "cache_line_bytes", "Cache_Line_Size");
  }
  // The axi5 object names both protocols that take it.
  auto other = base;
  other.initiators[1].axi5.emplace();
  CHECK(has(refused(other), "NCL_AXI5_PROPERTIES", "initiators[1].axi5",
            "protocol: axi5 or ace5_lite"));
}

TEST_CASE("Other coherent interface classes are refused", "[ace5-lite]") {
  using namespace nocloom;
  for (const auto *name : {"ace5_lite_dvm", "ace5_lite_acp", "ace5", "ace", "ace_lite"}) {
    INFO(name);
    const auto r = config::parse(std::regex_replace(with("ace5_lite"), std::regex("protocol: apb4"),
                                                    std::string("protocol: ") + name),
                                 "ace5_ports.yaml");
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_UNSUPPORTED_PROTOCOL");
    CHECK(r.diagnostics().front().message.find("Use ace5_lite") != std::string::npos);
  }
}

TEST_CASE("AxDOMAIN targets keep the memory type of AxCACHE", "[ace5-lite]") {
  using namespace nocloom;
  // A remapped AxCACHE could turn a Shareable request into a Device one (Issue L, table A8.7).
  auto zero = parsed(with("ace5_lite"));
  zero.targets[0].attributes = TargetAttributes::zero();
  CHECK(has(refused(zero), "NCL_TARGET_ATTRIBUTES", "targets[0].attributes", "AxDOMAIN"));
  // Without AxCACHE ports or without AxDOMAIN ports another mapping is allowed.
  auto no_cache = zero;
  no_cache.targets[0].axi5.emplace().cache = false;
  CHECK(generate(no_cache));
  auto plain = parsed(with("axi5"));
  plain.targets[0].attributes = TargetAttributes::zero();
  CHECK(generate(plain));
  // AxSNOOP ports without AxDOMAIN ports: an initiator admits only opcode zero, and a target
  // drives zero.
  const auto snoop = generate(parsed(with("axi5", "{write_snoop_width: 4, read_snoop_width: 4}")));
  REQUIRE(snoop);
  const auto &rtl = artifact(snoop.value(), ".v");
  for (const auto *declaration :
       {"input wire [3:0] nl_i_cpu_awsnoop,", "input wire [3:0] nl_i_cpu_arsnoop,",
        "output wire [3:0] nl_t_ram_awsnoop,", "output wire [3:0] nl_t_ram_arsnoop,"})
    CHECK(port(rtl, declaration));
  CHECK_FALSE(port(rtl, "domain"));
}

TEST_CASE("Initiators with AxDOMAIN or AxSNOOP ports keep several bursts in flight",
          "[ace5-lite]") {
  using namespace nocloom;
  // The destination decode of a tracked burst reads the admitted AxDOMAIN and AxSNOOP pair, and
  // the network's own decode check must see both of its values.
  for (const auto &[protocol, object] : {std::pair{"ace5_lite", "{}"},
                                         {"axi5", "{shareable: true}"},
                                         {"axi5", "{write_snoop_width: 4}"},
                                         {"axi5", "{read_snoop_width: 4}"}}) {
    INFO(protocol << " " << object);
    const auto text = std::regex_replace(with(protocol, object), std::regex("data_width: 32\\}"),
                                         "data_width: 32, max_outstanding: 4}");
    const auto d = parsed(text);
    REQUIRE(d.transport);
    CHECK(d.transport->max_outstanding == 4);
    CHECK(validate(d));
    CHECK(generate(d));
  }
}
