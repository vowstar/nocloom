#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <fstream>
#include <functional>
#include <map>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <regex>
#include <set>
#include <sstream>
#include <string>

namespace {
// Two initiators of different widths and two targets of different protocols. The same text with
// axi4 in place of axi5 gives the expected hardware.
const char *const yaml = R"(schema_version: 10
name: axi5_ports
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: PORT, data_width: 64, id_width: 3, len_width: 8,
     max_outstanding: 4, user_width: 2, user_flags: {owner: 'user[1]'}, response_user_width: 3}
  - {name: dma, clock_domain: main, protocol: PORT, data_width: 32, user_flags: {owner: 1}}
targets:
  - {name: ram, clock_domain: main, protocol: PORT, data_width: 32, len_width: 8,
     attributes: amba, user: [owner], response_user_width: 3,
     mappings: [{base: '0x0', size: '0x10000'}]}
  - {name: uart, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x10000', size: '0x1000'}]}
transport: {data_width: 32, user_flags: [owner], response_user_bits: 3}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
std::string with(const std::string &protocol) {
  return std::regex_replace(yaml, std::regex("PORT"), protocol);
}
nocloom::Definition parsed(const std::string &text) {
  const auto result = nocloom::config::parse(text, "axi5_ports.yaml");
  REQUIRE(result);
  return result.value().definition;
}
const std::string &artifact(const nocloom::ArtifactSet &set, const std::string &suffix) {
  const auto at =
      std::ranges::find_if(set, [&](const auto &a) { return a.path.ends_with(suffix); });
  REQUIRE(at != set.end());
  return at->text;
}
// The diagnostics of a refused definition.
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
} // namespace

TEST_CASE("AXI5 endpoints without properties generate the AXI4 hardware", "[axi5]") {
  using namespace nocloom;
  const auto axi5 = parsed(with("axi5")), axi4 = parsed(with("axi4"));
  CHECK(axi5.initiators[0].protocol == Protocol::Axi5);
  CHECK_FALSE(axi5.initiators[0].axi5);
  GenerationOptions options;
  options.ipxact = true;
  const auto a = generate(axi5, options), b = generate(axi4, options);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(artifact(a.value(), ".v") == artifact(b.value(), ".v"));
  // An explicit object of defaults is the same definition hardware.
  auto explicit_defaults = axi5;
  for (auto &i : explicit_defaults.initiators)
    if (i.protocol == Protocol::Axi5)
      i.axi5 = Axi5Properties{};
  explicit_defaults.targets[0].axi5 = Axi5Properties{};
  const auto c = generate(explicit_defaults, options);
  REQUIRE(c);
  CHECK(artifact(c.value(), ".v") == artifact(a.value(), ".v"));
  const auto &map = artifact(a.value(), ".map.json");
  for (const auto *name : {R"("cpu": "axi5")", R"("dma": "axi5")", R"("ram": "axi5")"})
    CHECK(map.find(name) != std::string::npos);
  CHECK(artifact(b.value(), ".map.json").find(R"(": "axi5")") == std::string::npos);
  const auto &xml = artifact(a.value(), ".xml");
  CHECK(xml.find(R"(spirit:library="AMBA5" spirit:name="AXI5" spirit:version="r0p0_0")") !=
        std::string::npos);
  CHECK(xml.find(R"(spirit:library="AMBA4" spirit:name="AXI4")") == std::string::npos);
}

TEST_CASE("AXI5 properties outside the supported set are refused", "[axi5]") {
  using namespace nocloom;
  const auto base = parsed(with("axi5"));
  const auto source = [&](const std::function<void(Axi5Properties &)> &edit) {
    auto d = base;
    d.initiators[0].axi5 = Axi5Properties{};
    edit(*d.initiators[0].axi5);
    return refused(d);
  };
  const auto target = [&](const std::function<void(Axi5Properties &)> &edit) {
    auto d = base;
    d.targets[0].axi5 = Axi5Properties{};
    edit(*d.targets[0].axi5);
    return refused(d);
  };
  // The object needs an axi5 endpoint.
  {
    auto d = parsed(with("axi4"));
    d.targets[0].axi5 = Axi5Properties{};
    CHECK(has(refused(d), "NCL_AXI5_PROPERTIES", "targets[0].axi5", "protocol: axi5"));
  }
  CHECK(has(source([](auto &p) { p.atomic = true; }), "NCL_AXI5_UNSUPPORTED",
            "initiators[0].axi5.atomic", "Atomic_Transactions"));
  CHECK(has(target([](auto &p) { p.dvm = Axi5Dvm::Receiver; }), "NCL_AXI5_UNSUPPORTED",
            "targets[0].axi5.dvm", "DVM_v9.2"));
  CHECK(has(source([](auto &p) { p.flow_control = Axi5FlowControl::Credited; }),
            "NCL_AXI5_UNSUPPORTED", "initiators[0].axi5.flow_control", "Shared_Credits_W"));
  CHECK(has(source([](auto &p) { p.cache_line_bytes = 128; }), "NCL_AXI5_UNSUPPORTED",
            "initiators[0].axi5.cache_line_bytes", "Cache_Line_Size"));
  CHECK(has(source([](auto &p) { p.cache_line_bytes = 96; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.cache_line_bytes"));
  CHECK(has(source([](auto &p) { p.subsystem_id_width = 9; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.subsystem_id_width"));
  CHECK(has(source([](auto &p) { p.max_transaction_bytes = 32; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.max_transaction_bytes"));
  CHECK(has(source([](auto &p) { p.write_loop_width = 9; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.write_loop_width"));
  CHECK(has(source([](auto &p) { p.pas_width = 4; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.pas_width"));
  CHECK(has(source([](auto &p) { p.read_snoop_width = 5; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.read_snoop_width"));
  // IHI 0022 Issue L A4.5.1: AxPROT or the AxPRIV, AxINST and AxPAS signals.
  CHECK(has(source([](auto &p) { p.priv_inst = true; }), "NCL_AXI5_PROPERTIES",
            "initiators[0].axi5.prot"));
  // A target issues bursts of every shape.
  for (const auto &[key, member] : {std::pair{"regular_only", &Axi5Properties::regular_only},
                                    std::pair{"wrap_line_only", &Axi5Properties::wrap_line_only},
                                    std::pair{"no_fixed_bursts", &Axi5Properties::no_fixed_bursts}})
    CHECK(has(target([member](Axi5Properties &p) { p.*member = true; }), "NCL_AXI5_UNSUPPORTED",
              std::string("targets[0].axi5.") + key));
  // An initiator only declares them.
  {
    auto d = base;
    d.initiators[0].axi5 = Axi5Properties{};
    auto &p = *d.initiators[0].axi5;
    p.regular_only = p.wrap_line_only = p.no_fixed_bursts = p.device_normal_independence = true;
    p.max_transaction_bytes = 64;
    CHECK(generate(d));
  }
}

TEST_CASE("A target ID port has at most 31 bits and its diagnostic names the protocol", "[axi5]") {
  using namespace nocloom;
  for (const auto &[protocol, name] : std::map<Protocol, std::string>{
           {Protocol::Axi5, "AXI5"}, {Protocol::Axi4, "AXI4"}, {Protocol::Axi3, "AXI3"}}) {
    auto d = parsed(with("axi4"));
    d.targets[0].protocol = protocol;
    // An AXI3 length port has four bits.
    d.targets[0].len_width.reset();
    d.targets[0].id_width = 31;
    CHECK(generate(d));
    d.targets[0].id_width = 32;
    const auto list = refused(d);
    CHECK(has(list, "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[0].id_width",
              name + " target ID ports have 0 to 31 bits"));
    CHECK_FALSE(has(list, "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[0].id_width", "Only"));
  }
  auto d = parsed(with("axi5"));
  d.targets[0].len_width = 9;
  CHECK(has(refused(d), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[0].len_width",
            "AXI5 target length ports have 4 to 8 bits"));
  d = parsed(with("axi5"));
  d.targets[1].id_width = 4;
  CHECK(has(refused(d), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[1].id_width",
            "Only full AXI targets have ID ports"));
}

TEST_CASE("Exclusive diagnostics name AXI5 among the protocols with AxLOCK", "[axi5]") {
  using namespace nocloom;
  auto d = parsed(with("axi5"));
  d.targets[0].exclusive = ExclusiveAccess::Target;
  d.targets[0].exclusive_owner_flags = {"owner"};
  CHECK(generate(d));
  d.targets[1].exclusive = ExclusiveAccess::Target;
  CHECK(has(refused(d), "NCL_TARGET_EXCLUSIVE", "targets[1].exclusive",
            "Only AXI5, AXI4 and AXI3 targets have AxLOCK"));
  d = parsed(with("axi5"));
  d.targets[0].exclusive = ExclusiveAccess::Network;
  for (auto &i : d.initiators)
    i.access = Access::ReadOnly;
  CHECK(has(refused(d), "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive",
            "No read-write AXI5, AXI4 or AXI3 initiator"));
}

TEST_CASE("AXI5 constraint properties hold for every connected pair", "[axi5]") {
  using namespace nocloom;
  const auto base = parsed(with("axi5"));
  auto d = base;
  d.initiators[0].axi5 = Axi5Properties{};
  d.initiators[0].axi5->consistent_decerr = true;
  // The AXI target can return DECERR on part of a cache line. The APB target cannot.
  const auto list = refused(d);
  CHECK(has(list, "NCL_AXI5_PROPERTIES", "initiators[0].axi5.consistent_decerr", "Target ram"));
  CHECK_FALSE(
      has(list, "NCL_AXI5_PROPERTIES", "initiators[0].axi5.consistent_decerr", "Target uart"));
  d.targets[0].axi5 = Axi5Properties{};
  d.targets[0].axi5->consistent_decerr = true;
  CHECK(generate(d));
  // A mapping below one cache line leaves a decode miss inside a line.
  auto small = d;
  small.targets[1].mappings[0].size = 0x20;
  CHECK(has(refused(small), "NCL_AXI5_PROPERTIES", "initiators[0].axi5.consistent_decerr",
            "Target uart"));
  // SLVERR for every failure also meets the property.
  auto slverr = base;
  slverr.initiators[0].axi5 = Axi5Properties{};
  slverr.initiators[0].axi5->consistent_decerr = true;
  slverr.initiators[0].slverr_only = true;
  CHECK(generate(slverr));
  // Device_Normal_Independence at a target needs it at each reaching initiator.
  auto order = base;
  order.targets[0].axi5 = Axi5Properties{};
  order.targets[0].axi5->device_normal_independence = true;
  order.initiators[0].axi5 = order.targets[0].axi5;
  CHECK(has(refused(order), "NCL_AXI5_PROPERTIES", "targets[0].axi5.device_normal_independence",
            "Initiator dma"));
  order.initiators[1].axi5 = order.targets[0].axi5;
  CHECK(generate(order));
}

TEST_CASE("The axi5 object reads every field and refuses other interface classes", "[axi5]") {
  using namespace nocloom;
  const auto read = [](const std::string &object) {
    return config::parse(
        std::regex_replace(with("axi5"), std::regex("protocol: axi5, data_width: 32, user"),
                           "protocol: axi5, data_width: 32, axi5: " + object + ", user"),
        "axi5_ports.yaml");
  };
  const auto all = read("{mpam: partid_12, subsystem_id_width: 8, nsaid: true, pbha: true, "
                        "trace: true, poison: true, unique_id: true, write_loop_width: 3, "
                        "read_loop_width: 4, prot: false, priv_inst: true, pas_width: 1, "
                        "cache: false, max_transaction_bytes: 256, mte: standard, "
                        "untranslated: v4, cache_stash: full, interface_check: all, dvm: "
                        "receiver, flow_control: credited, shareable: true, write_snoop_width: "
                        "5, read_snoop_width: 4, cache_line_bytes: 128}");
  REQUIRE(all);
  const auto &p = *all.value().definition.initiators[1].axi5;
  CHECK(p.mpam == Axi5Mpam::PartId12);
  CHECK(p.subsystem_id_width == 8);
  CHECK((p.nsaid && p.pbha && p.trace && p.poison && p.unique_id && !p.prot && p.priv_inst &&
         !p.cache));
  CHECK(p.write_loop_width == 3);
  CHECK(p.read_loop_width == 4);
  CHECK(p.pas_width == 1);
  CHECK(p.max_transaction_bytes == 256);
  CHECK(p.mte == Axi5Tagging::Standard);
  CHECK(p.untranslated == Axi5Untranslated::V4);
  CHECK(p.cache_stash == Axi5CacheStash::Full);
  CHECK(p.interface_check == Axi5InterfaceCheck::All);
  CHECK(p.dvm == Axi5Dvm::Receiver);
  CHECK(p.flow_control == Axi5FlowControl::Credited);
  CHECK(p.shareable == true);
  CHECK(p.write_snoop_width == 5U);
  CHECK(p.read_snoop_width == 4U);
  CHECK(p.cache_line_bytes == 128);
  const auto code = [&](const std::string &object) {
    const auto r = read(object);
    REQUIRE_FALSE(r);
    return r.diagnostics().front().code;
  };
  CHECK(code("{mpam: partid_10}") == "NCL_AXI5_PROPERTIES");
  CHECK(code("{region: 1}") == "NCL_SCHEMA_UNKNOWN_FIELD");
  CHECK(code("{trace: 1}") == "NCL_SCHEMA_TYPE");
  for (const auto *name : {"axi5_lite", "ace5_lite", "ace5_lite_dvm", "ace5_lite_acp"}) {
    const auto r = config::parse(std::regex_replace(with("axi5"), std::regex("protocol: apb4"),
                                                    std::string("protocol: ") + name),
                                 "axi5_ports.yaml");
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_UNSUPPORTED_PROTOCOL");
    CHECK(r.diagnostics().front().message.find("interface class") != std::string::npos);
  }
}

// Each rejected property of the manual's property table names a field whose value the validator
// refuses with a diagnostic that names the property.
TEST_CASE("Each rejected property of the manual has a diagnostic", "[axi5]") {
  using namespace nocloom;
  const auto read = [](const char *page) {
    std::ifstream in(std::string(NOCLOOM_SOURCE_DIR) + "/docs/" + page);
    std::stringstream text;
    text << in.rdbuf();
    return text.str();
  };
  const auto manual = read("axi5.md"), properties = read("axi5-properties.md");
  // The field table: the field and the properties each one declares.
  std::map<std::string, std::string> declared;
  const std::regex field_row(R"(\n\| `([a-z_0-9]+)` \| ([^|]+) \|)");
  const auto fields = manual.substr(0, manual.find("## Constraints"));
  for (std::sregex_iterator it(fields.begin(), fields.end(), field_row), end; it != end; ++it)
    declared[(*it)[1]] = (*it)[2];
  const auto start = properties.find("## Rejected properties");
  REQUIRE(start != std::string::npos);
  const auto rejected = properties.substr(start, properties.find("## Interface classes") - start);
  const std::regex rejected_row(R"(\n\| ([A-Za-z_0-9.]+) \| )");
  unsigned rows = 0;
  for (std::sregex_iterator it(rejected.begin(), rejected.end(), rejected_row), end; it != end;
       ++it) {
    const std::string property = (*it)[1];
    if (property == "Property")
      continue;
    ++rows;
    // The field that declares the property and a value that enables it.
    const auto field = std::ranges::find_if(declared, [&](const auto &row) {
      return std::regex_search(
          row.second,
          std::regex("(^|[ ,])" + std::regex_replace(property, std::regex(R"(\.)"), R"(\.)") +
                     "($|[ ,])"));
    });
    if (property == "USER_DATA_WIDTH" || property == "REGION_Present") {
      CHECK(field == declared.end());
      continue;
    }
    INFO(property);
    REQUIRE(field != declared.end());
    static const std::map<std::string, std::string> values{
        {"mpam", "partid_9"},        {"subsystem_id_width", "1"},  {"write_loop_width", "1"},
        {"read_loop_width", "1"},    {"pas_width", "2"},           {"mte", "basic"},
        {"interface_check", "data"}, {"untranslated", "v1"},       {"cache_stash", "basic"},
        {"dvm", "receiver"},         {"flow_control", "credited"}, {"write_snoop_width", "4"},
        {"read_snoop_width", "4"},   {"cache_line_bytes", "128"},  {"prot", "false"},
        {"cache", "false"}};
    auto key = field->first;
    // PAS_WIDTH of one bit needs prot: false.
    std::string object = "{" + key + ": " +
                         (values.contains(key) ? values.at(key) : std::string("true")) +
                         (key == "pas_width" || key == "priv_inst" ? ", prot: false" : "") + "}";
    if (property == "PAS_WIDTH")
      object = "{pas_width: 1, prot: false}";
    const auto r = config::parse(
        std::regex_replace(with("axi5"), std::regex("protocol: axi5, data_width: 32, user"),
                           "protocol: axi5, data_width: 32, axi5: " + object + ", user"),
        "axi5_ports.yaml");
    REQUIRE(r);
    const auto generated = generate(r.value().definition);
    REQUIRE_FALSE(generated);
    CHECK(std::ranges::any_of(generated.diagnostics(), [&](const Diagnostic &x) {
      return x.code == "NCL_AXI5_UNSUPPORTED" &&
             (x.message.find(property) != std::string::npos ||
              x.message.find(field->second.substr(0, field->second.find(','))) !=
                  std::string::npos);
    }));
  }
  CHECK(rows > 50);
  // The property table lists each of the 97 properties of the specification once.
  const auto &table = properties;
  const std::regex any_row(R"(\n\| ([A-Z][A-Za-z_0-9.]+) \| )");
  std::set<std::string> names;
  unsigned count = 0;
  for (std::sregex_iterator it(table.begin(), table.end(), any_row), end; it != end; ++it)
    if (const std::string name = (*it)[1]; name != "Property" && name != "Class" &&
                                           name != "Pass" && name != "Interpreted" &&
                                           name != "Rejected") {
      names.insert(name);
      ++count;
    }
  CHECK(count == 97);
  CHECK(names.size() == 97);
}

TEST_CASE("AXI5 request attribute ports follow the declared properties", "[axi5]") {
  using namespace nocloom;
  auto d = parsed(with("axi5"));
  auto &cpu = d.initiators[0].axi5.emplace();
  cpu.mpam = Axi5Mpam::PartId12;
  cpu.subsystem_id_width = 3;
  cpu.nsaid = cpu.pbha = true;
  cpu.prot = cpu.cache = false;
  cpu.priv_inst = true;
  cpu.pas_width = 1;
  d.initiators[1].axi5.emplace().subsystem_id = 9;
  auto &ram = d.targets[0].axi5.emplace();
  ram.mpam = Axi5Mpam::PartId12;
  ram.subsystem_id_width = 4;
  ram.nsaid = true;
  ram.prot = false;
  ram.pas_width = 1;
  const auto r = generate(d);
  REQUIRE(r);
  const auto &rtl = artifact(r.value(), ".v");
  for (const auto *port :
       {"[13:0] nl_i_cpu_awmpam,", "[2:0] nl_i_cpu_arsubsysid,", "[3:0] nl_i_cpu_awnsaid,",
        "[3:0] nl_i_cpu_arpbha,", " nl_i_cpu_awpriv,", " nl_i_cpu_arinst,", " nl_i_cpu_awpas,",
        "[13:0] nl_t_ram_armpam,", "[3:0] nl_t_ram_awsubsysid,", "[3:0] nl_t_ram_arnsaid,",
        " nl_t_ram_awpas,", "[3:0] nl_t_ram_arcache,"})
    CHECK(rtl.find(port) != std::string::npos);
  for (const auto *absent : {"nl_i_cpu_awprot", "nl_i_cpu_awcache", "nl_t_ram_awprot",
                             "nl_t_ram_awpriv", "nl_t_ram_awpbha", "nl_i_dma_awmpam"})
    CHECK(rtl.find(absent) == std::string::npos);
  // A target port holds the attributes of each initiator that reaches it.
  auto narrow = d;
  narrow.targets[0].axi5->subsystem_id_width = 2;
  CHECK(has(refused(narrow), "NCL_AXI5_PROPERTIES", "targets[0].axi5.subsystem_id_width",
            "initiator cpu"));
  narrow.targets[0].axi5->subsystem_id_width = 3;
  CHECK(has(refused(narrow), "NCL_AXI5_PROPERTIES", "targets[0].axi5.subsystem_id_width",
            "initiator dma"));
  auto partid = d;
  partid.targets[0].axi5->mpam = Axi5Mpam::PartId9;
  CHECK(has(refused(partid), "NCL_AXI5_PROPERTIES", "targets[0].axi5.mpam", "initiator cpu"));
  // The constant subsystem ID belongs to an initiator without the port.
  auto constant = d;
  constant.initiators[0].axi5->subsystem_id = 1;
  CHECK(has(refused(constant), "NCL_AXI5_PROPERTIES", "initiators[0].axi5.subsystem_id"));
  constant = d;
  constant.targets[0].axi5->subsystem_id = 1;
  CHECK(has(refused(constant), "NCL_AXI5_PROPERTIES", "targets[0].axi5.subsystem_id"));
  // A mapping for a signal the target omits would be ignored.
  auto mapping = d;
  mapping.targets[0].axi5->cache = false;
  mapping.targets[0].attributes->read_cache[0] = {AttributeSource::One};
  CHECK(has(refused(mapping), "NCL_TARGET_ATTRIBUTES", "targets[0].attributes"));
  mapping = d;
  mapping.targets[0].axi5->pas_width = 0;
  mapping.targets[0].attributes->prot[0] = {AttributeSource::One};
  CHECK(has(refused(mapping), "NCL_TARGET_ATTRIBUTES", "targets[0].attributes"));
  mapping.targets[0].attributes = TargetAttributes::zero();
  CHECK(generate(mapping));
}

TEST_CASE("Targets drive AxREGION from the address map and AxQOS from the level", "[axi5]") {
  using namespace nocloom;
  auto d = parsed(with("axi4"));
  d.targets[0].region_port = true;
  d.targets[0].mappings[0].region = 7;
  d.targets[0].axqos_levels = {0, 1, 2, 3, 4, 5, 6, 7};
  const auto r = generate(d);
  REQUIRE(r);
  const auto &rtl = artifact(r.value(), ".v");
  for (const auto *port : {"[3:0] nl_t_ram_awregion,", "[3:0] nl_t_ram_arregion,",
                           "[3:0] nl_t_ram_awqos,", "[3:0] nl_t_ram_arqos,"})
    CHECK(rtl.find(port) != std::string::npos);
  CHECK(rtl.find("nl_i_cpu_awregion") == std::string::npos);
  const auto refused_at = [&](auto edit, const std::string &code, const std::string &field) {
    auto bad = d;
    edit(bad);
    CHECK(has(refused(bad), code, field));
  };
  refused_at([](Definition &x) { x.targets[0].mappings[0].region = 16; }, "NCL_TARGET_REGION",
             "targets[0].mappings[0].region");
  refused_at([](Definition &x) { x.targets[0].region_port = false; }, "NCL_TARGET_REGION",
             "targets[0].mappings[0].region");
  refused_at([](Definition &x) { x.targets[1].region_port = true; }, "NCL_TARGET_REGION",
             "targets[1].region_port");
  refused_at([](Definition &x) { x.targets[0].axqos_levels.pop_back(); }, "NCL_QOS_MAP",
             "targets[0].axqos_levels");
  refused_at([](Definition &x) { x.targets[0].axqos_levels[3] = 16; }, "NCL_QOS_MAP",
             "targets[0].axqos_levels");
  refused_at([](Definition &x) { x.targets[0].axqos_levels[3] = 0; }, "NCL_QOS_MAP",
             "targets[0].axqos_levels");
  refused_at([](Definition &x) { x.targets[1].axqos_levels.assign(8, 1); }, "NCL_QOS_MAP",
             "targets[1].axqos_levels");
  // An initiator has no AxREGION port (IHI 0022 Issue L, A4.7.1).
  const auto parsed_region = config::parse(
      std::regex_replace(with("axi5"), std::regex("protocol: axi5, data_width: 32, user"),
                         "protocol: axi5, data_width: 32, region_port: true, user"),
      "axi5_ports.yaml");
  REQUIRE_FALSE(parsed_region);
  CHECK(parsed_region.diagnostics().front().code == "NCL_TARGET_REGION");
  // A wider initiator with an AxQOS map loses its level in the width clamp.
  auto wide = d;
  wide.initiators[0].qos.axqos_map = {0, 1, 2, 3};
  wide.initiators[0].max_outstanding.reset();
  wide.initiators[1].qos.levels = {1, 1};
  CHECK(has(refused(wide), "NCL_QOS_MAP", "targets[0].axqos_levels", "Initiator cpu"));
}
