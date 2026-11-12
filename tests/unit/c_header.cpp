#include "../graph_example_definition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <cctype>
#include <cstdint>
#include <fstream>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <sstream>
#include <string>
#include <vector>

using namespace nocloom;
namespace {
Definition definition() {
  auto d = graph_example_definition();
  d.name = "headers";
  d.topology.reset();
  return d;
}
const std::string &text(const ArtifactSet &set, std::string_view suffix) {
  const auto at =
      std::ranges::find_if(set, [&](const Artifact &a) { return a.path.ends_with(suffix); });
  REQUIRE(at != set.end());
  return at->text;
}
ArtifactSet headers(const Definition &d) {
  const auto result = generate(d, {.c_header = true});
  std::string messages;
  for (const auto &diagnostic : result.diagnostics())
    messages += diagnostic.code + ": " + diagnostic.message + "\n";
  INFO(messages);
  REQUIRE(result);
  return result.value();
}
Definition example(const std::string &name) {
  std::ifstream input(std::string(NOCLOOM_SOURCE_DIR) + "/examples/" + name + ".yaml");
  std::ostringstream data;
  data << input.rdbuf();
  const auto parsed = config::parse(data.str());
  REQUIRE(parsed);
  return parsed.value().definition;
}
void value(const ArtifactSet &set, const std::string &header, const std::string &macro,
           const std::string &expected) {
  INFO(macro);
  CHECK(text(set, header).find("#define " + macro + " " + expected + "\n") != std::string::npos);
}
void absent(const ArtifactSet &set, const std::string &header, const std::string &macro) {
  INFO(macro);
  CHECK(text(set, header).find("#define " + macro + " ") == std::string::npos);
}
// The number a header gives a macro.
std::uint64_t number(const ArtifactSet &set, const std::string &header, const std::string &macro) {
  const auto &all = text(set, header);
  const auto lead = "#define " + macro + " (0x";
  const auto at = all.find(lead);
  INFO(macro);
  REQUIRE(at != std::string::npos);
  return std::stoull(all.substr(at + lead.size()), nullptr, 16);
}
std::string hex(std::uint64_t quantity) {
  std::ostringstream out;
  out << "(0x" << std::hex << quantity << "U)";
  return out.str();
}
std::string upper(std::string name) {
  std::ranges::transform(name, name.begin(),
                         [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  return name;
}
// The register table of an error log bank, layout 2, written out here: name, byte offset of the
// word, lowest bit, width.
struct Place {
  std::string name;
  unsigned offset = 0, lsb = 0, width = 0;
};
std::vector<Place> layout2() {
  std::vector<Place> fields{{"CAPABILITY_AUDIT_FLAGS", 0x00, 0, 1},
                            {"CAPABILITY_USER_FLAGS", 0x00, 1, 1},
                            {"CAPABILITY_TARGET_USER_FLAGS", 0x00, 2, 1},
                            {"CAPABILITY_NEXT_LOGGER", 0x00, 3, 1},
                            {"CAPABILITY_GIVEN_REVISION", 0x00, 4, 1},
                            {"LOGGER_INDEX", 0x00, 8, 5},
                            {"LOG_INDEX", 0x00, 16, 6},
                            {"LAYOUT", 0x00, 24, 8},
                            {"RECORD_HELD", 0x04, 0, 1},
                            {"RECORD_OVERFLOW", 0x04, 1, 1},
                            {"RECORD_CLASS", 0x04, 4, 4},
                            {"ALARM_ENABLE", 0x04, 8, 1},
                            {"WAIT_WHEN_FULL", 0x04, 9, 1},
                            {"RECORD_WRITE", 0x04, 12, 1},
                            {"RECORD_WRAP", 0x04, 13, 1},
                            {"RECORD_EXCLUSIVE", 0x04, 14, 1},
                            {"RECORD_ADDRESS_PARTIAL", 0x04, 15, 1},
                            {"RECORD_SEQUENCE", 0x04, 16, 16},
                            {"RECORD_ADDRESS_LOW", 0x08, 0, 32},
                            {"RECORD_ADDRESS_HIGH", 0x0c, 0, 16},
                            {"RECORD_BYTES", 0x0c, 16, 16},
                            {"RECORD_SOURCE_PATH", 0x10, 0, 13},
                            {"RECORD_TARGET_PATH", 0x10, 16, 13},
                            {"RECORD_REGION_INDEX", 0x14, 0, 32},
                            {"RECORD_AUDIT_FLAGS", 0x18, 0, 32},
                            {"REVISION", 0x1c, 0, 32}};
  for (unsigned k = 0; k < 8; ++k)
    fields.push_back({"RECORD_USER_FLAGS" + std::to_string(k), 0x20 + 4 * k, 0, 32});
  return fields;
}
// Three APB sources and a window of error log banks at local address 0x1000. A smaller mapping
// of another target splits the window in two, and the source narrow reaches no bank.
Definition logged() {
  Definition d;
  d.schema_version = 10;
  d.name = "headers";
  d.address_width = 32;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu", "main"}, {"debug", "main"}, {"narrow", "main"}};
  for (auto &source : d.initiators) {
    source.protocol = Protocol::Apb4;
    source.data_width = 32;
  }
  d.initiators[2].address_width = 20;
  d.targets = {{"ram", "main", {{0, 0x1000}}},
               {"csr", "main", {{0x100000, 0x10000, 0x1000}}},
               {"hole", "main", {{0x100800, 0x800}}}};
  for (auto &target : d.targets) {
    target.protocol = Protocol::Apb4;
    target.data_width = 32;
    target.attributes = TargetAttributes::amba();
  }
  d.targets[1].log_registers = true;
  d.error_logging.logs = {
      {"fault", {"ram"}, std::nullopt, false, {{.offset = 0x1000}, {.offset = 0x2000}}}};
  d.connectivity = Connectivity{.pairs = {{"cpu", "ram"},
                                          {"cpu", "csr"},
                                          {"cpu", "hole"},
                                          {"debug", "ram"},
                                          {"debug", "csr"},
                                          {"debug", "hole"},
                                          {"narrow", "ram"}}};
  return d;
}
// The summary a map gives a bank of its first word.
void summary(const nlohmann::json &bank, unsigned log, unsigned logger,
             const std::vector<std::string> &parts) {
  INFO(bank.at("bank").get<std::string>());
  CHECK(bank.at("layout") == 2);
  CHECK(bank.at("log_index") == log);
  CHECK(bank.at("logger_index") == logger);
  CHECK(bank.at("capabilities") == nlohmann::json(parts));
}
} // namespace

TEST_CASE("C headers are optional and preserve existing artifacts", "[c-header]") {
  auto d = definition();
  const auto plain = generate(d);
  REQUIRE(plain);
  const auto generated = headers(d);
  REQUIRE(generated.size() == plain.value().size() + d.initiators.size());
  for (const auto &a : plain.value())
    if (!a.path.ends_with(".bundle.json"))
      CHECK(std::ranges::find(generated, a) != generated.end());
  const auto bundle = nlohmann::json::parse(text(generated, ".bundle.json"));
  unsigned count = 0;
  for (const auto &file : bundle.at("files"))
    if (file.at("role") == "c_header") {
      ++count;
      CHECK(std::ranges::any_of(generated, [&](const Artifact &a) {
        return a.path == file.at("path").get<std::string>();
      }));
    }
  CHECK(count == 3);
  CHECK(std::ranges::none_of(generated,
                             [](const Artifact &a) { return a.path.ends_with(".error_logs.h"); }));
  CHECK(generated == headers(d));
  std::ranges::reverse(d.initiators);
  std::ranges::reverse(d.targets);
  CHECK(generated == headers(d));
  CHECK(std::ranges::is_sorted(generated, {}, &Artifact::path));
  const auto &header = text(generated, ".s0.addresses.h");
  CHECK(header.starts_with("/* Generated by NocLoom */\n"));
  CHECK(header.find("LicenseRef-NocLoom-Output-1.0") != std::string::npos);
  CHECK(header.find("#ifndef NOCLOOM_HEADERS_S0_ADDRESS_H\n") != std::string::npos);
  CHECK(header.ends_with("#endif\n"));
  CHECK(header.find(std::string(version())) == std::string::npos);
}

TEST_CASE("C header windows include aliases and clipped 48-bit addresses", "[c-header]") {
  auto d = definition();
  d.address_width = 48;
  d.initiators[0].address_width = 32;
  d.targets[0].mappings = {{0xffff0000, 0x20000, 0}, {0x123450000000ULL, 0x1000, 0x2000}};
  d.targets[0].address_width = 17;
  d.initiators[1].default_target = "t0";
  const auto out = headers(d);
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_0_BASE", "(0xffff0000U)");
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_0_SIZE", "(0x10000U)");
  CHECK(text(out, ".s0.addresses.h").find("T0_WINDOW_1") == std::string::npos);
  value(out, ".s1.addresses.h", "NOCLOOM_HEADERS_S1_T0_WINDOW_0_SIZE", "(0x20000U)");
  value(out, ".s1.addresses.h", "NOCLOOM_HEADERS_S1_T0_WINDOW_1_BASE", "(0x123450000000ULL)");
  d.targets[1].mappings = {{0x100000, 0x1000, 0x1000}};
  value(headers(d), ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T1_WINDOW_0_BASE", "(0x100000U)");
  CHECK(text(out, ".s1.addresses.h").find("\"local_base\":\"0x000000002000\"") !=
        std::string::npos);
  CHECK(text(out, ".s1.addresses.h").find("\"default_target\":true") != std::string::npos);
  d.targets[0].mappings = {{0, 0x100000000ULL, 0}};
  d.targets[0].address_width = 32;
  d.targets.resize(1);
  value(headers(d), ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_0_SIZE", "(0x100000000ULL)");
}

TEST_CASE("C headers retain mode conditions and flow connectivity", "[c-header]") {
  for (const auto *name : {"mode_remap", "explicit_routes_conn_flows"}) {
    const auto d = example(name);
    const auto out = headers(d);
    const auto tables = add_address_table(out);
    REQUIRE(tables);
    const auto table = nlohmann::json::parse(text(tables.value(), ".address_table.json"));
    for (const auto &source : table.at("initiators")) {
      const auto &header = text(out, "." + source.at("name").get<std::string>() + ".addresses.h");
      for (const auto &window : source.at("windows"))
        CHECK(header.find("/* " + window.dump() + " */") != std::string::npos);
    }
    if (std::string(name) == "mode_remap") {
      const auto &header = text(out, ".a0.addresses.h");
      CHECK(header.find("\"when\":") != std::string::npos);
    } else {
      const auto &header = text(out, ".i1.addresses.h");
      CHECK(header.find("\"flow\":1") != std::string::npos);
      CHECK(header.find("\"source_paths\":[0]") != std::string::npos);
      value(out, ".i1.addresses.h", "NOCLOOM_EXPLICIT_ROUTES_CONN_FLOWS_I1_T0_WINDOW_0_BASE",
            "(0x1000U)");
      CHECK(header.find("T0_WINDOW_1") == std::string::npos);
    }
  }
}

TEST_CASE("C macro conflicts fail generation without artifacts", "[c-header]") {
  auto d = definition();
  d.targets[1].name = "T0";
  REQUIRE(generate(d));
  const auto result = generate(d, {.c_header = true});
  REQUIRE_FALSE(result);
  REQUIRE(result.diagnostics().size() == 1);
  CHECK(result.diagnostics()[0].code == "NCL_C_HEADER_NAME");
  CHECK(result.diagnostics()[0].message.find("NOCLOOM_HEADERS_S0_T0_WINDOW_0_BASE") !=
        std::string::npos);
  d = definition();
  d.initiators[1].name = "S0";
  REQUIRE(generate(d));
  const auto guard = generate(d, {.c_header = true});
  REQUIRE_FALSE(guard);
  CHECK(guard.diagnostics()[0].code == "NCL_C_HEADER_NAME");
  // The revision field of a cut bank x and the offset of a whole bank x_revision are one macro.
  d = logged();
  d.targets[2].mappings = {{0x1007d0, 0x8}};
  d.targets.push_back(d.targets[0]);
  d.targets[3].name = "rom";
  d.targets[3].mappings = {{0x200000, 0x1000}};
  d.connectivity->pairs.push_back({"cpu", "rom"});
  d.error_logging.logs = {{"fault", {"ram"}, std::nullopt, false, {{.offset = 0x1000}}},
                          {"x", {"hole"}, std::nullopt, false, {{.offset = 0x17c0}}},
                          {"x_revision", {"rom"}, std::nullopt, false, {{.offset = 0x2000}}}};
  REQUIRE(generate(d));
  const auto field = generate(d, {.c_header = true});
  REQUIRE_FALSE(field);
  REQUIRE(field.diagnostics().size() == 1);
  CHECK(field.diagnostics()[0].code == "NCL_C_HEADER_NAME");
  CHECK(field.diagnostics()[0].message.find("NOCLOOM_HEADERS_CPU_CSR_WINDOW_1_X_REVISION_OFFSET") !=
        std::string::npos);
  d.error_logging.logs[2].name = "y_revision";
  const auto apart = headers(d);
  value(apart, ".error_logs.h", "NOCLOOM_HEADERS_CPU_CSR_WINDOW_1_X_REVISION_OFFSET", "(0x4U)");
  value(apart, ".error_logs.h", "NOCLOOM_HEADERS_CPU_CSR_WINDOW_1_Y_REVISION_OFFSET", "(0x828U)");
}

TEST_CASE("C logger macros state the layout once and the offset of every bank", "[c-header]") {
  auto d = definition();
  d.initiators.resize(1);
  d.initiators[0].name = "error_logs";
  d.targets.resize(2);
  for (auto &source : d.initiators) {
    source.protocol = Protocol::Apb4;
    source.len_width.reset();
  }
  for (auto &target : d.targets) {
    target.protocol = Protocol::Apb4;
    target.len_width.reset();
  }
  d.targets[1].name = "csr";
  d.targets[1].mappings = {{0x100000, 0x100000}};
  d.targets[1].log_registers = true;
  d.error_logging.logs = {
      {"fault",
       {"t0"},
       std::nullopt,
       false,
       {{.offset = 0x200}, {.offset = 0x500}, {.offset = 0x1000}, {.offset = 0xfffc0}}}};
  auto out = headers(d);
  CHECK_FALSE(text(out, ".error_logs.addresses.h").empty());
  const std::string layout = "NOCLOOM_HEADERS_ERROR_LOG_";
  const std::string window = "NOCLOOM_HEADERS_ERROR_LOGS_CSR_WINDOW_0_";
  // The layout: its number, a bank's bytes, the classes, and each field's word, shift and mask.
  value(out, ".error_logs.h", layout + "LAYOUT", "(0x2U)");
  value(out, ".error_logs.h", layout + "BANK_BYTES", "(0x40U)");
  value(out, ".error_logs.h", layout + "CLASS_DECODE", "(0x1U)");
  value(out, ".error_logs.h", layout + "CLASS_TARGET", "(0x2U)");
  const auto &header = text(out, ".error_logs.h");
  std::size_t place = 0;
  for (const auto &field : layout2()) {
    const auto mask = (field.width == 32 ? 0xffffffffULL : (1ULL << field.width) - 1) << field.lsb;
    value(out, ".error_logs.h", layout + field.name + "_OFFSET", hex(field.offset));
    value(out, ".error_logs.h", layout + field.name + "_SHIFT", hex(field.lsb));
    value(out, ".error_logs.h", layout + field.name + "_MASK", hex(mask));
    // The fields stand in the order of the table.
    const auto at = header.find("#define " + layout + field.name + "_OFFSET ");
    CHECK((at != std::string::npos && at > place));
    place = at;
  }
  value(out, ".error_logs.h", layout + "LAYOUT_SHIFT", "(0x18U)");
  value(out, ".error_logs.h", layout + "LAYOUT_MASK", "(0xff000000U)");
  value(out, ".error_logs.h", layout + "LOG_INDEX_SHIFT", "(0x10U)");
  value(out, ".error_logs.h", layout + "LOG_INDEX_MASK", "(0x3f0000U)");
  value(out, ".error_logs.h", layout + "LOGGER_INDEX_SHIFT", "(0x8U)");
  value(out, ".error_logs.h", layout + "LOGGER_INDEX_MASK", "(0x1f00U)");
  value(out, ".error_logs.h", layout + "RECORD_HELD_SHIFT", "(0x0U)");
  value(out, ".error_logs.h", layout + "RECORD_HELD_MASK", "(0x1U)");
  value(out, ".error_logs.h", layout + "RECORD_CLASS_SHIFT", "(0x4U)");
  value(out, ".error_logs.h", layout + "RECORD_CLASS_MASK", "(0xf0U)");
  value(out, ".error_logs.h", layout + "RECORD_BYTES_OFFSET", "(0xcU)");
  value(out, ".error_logs.h", layout + "RECORD_BYTES_SHIFT", "(0x10U)");
  value(out, ".error_logs.h", layout + "RECORD_BYTES_MASK", "(0xffff0000U)");
  value(out, ".error_logs.h", layout + "RECORD_TARGET_PATH_OFFSET", "(0x10U)");
  value(out, ".error_logs.h", layout + "RECORD_TARGET_PATH_SHIFT", "(0x10U)");
  value(out, ".error_logs.h", layout + "RECORD_TARGET_PATH_MASK", "(0x1fff0000U)");
  value(out, ".error_logs.h", layout + "RECORD_USER_FLAGS7_OFFSET", "(0x3cU)");
  value(out, ".error_logs.h", layout + "RECORD_USER_FLAGS7_SHIFT", "(0x0U)");
  value(out, ".error_logs.h", layout + "RECORD_USER_FLAGS7_MASK", "(0xffffffffU)");
  // Each bank: its offset in the window and what its first word reads.
  value(out, ".error_logs.h", window + "CSR_OFFSET", "(0x200U)");
  value(out, ".error_logs.h", window + "FAULT_1_OFFSET", "(0x500U)");
  value(out, ".error_logs.h", window + "FAULT_2_OFFSET", "(0x1000U)");
  value(out, ".error_logs.h", window + "FAULT_3_OFFSET", "(0xfffc0U)");
  value(out, ".error_logs.h", window + "CSR_ID_VALUE", "(0x2000008U)");
  value(out, ".error_logs.h", window + "FAULT_1_ID_VALUE", "(0x2000108U)");
  value(out, ".error_logs.h", window + "FAULT_2_ID_VALUE", "(0x2000208U)");
  value(out, ".error_logs.h", window + "FAULT_3_ID_VALUE", "(0x2000300U)");
  // A register is at the window's base plus the bank's offset plus the field's offset. The state
  // word holds the status bits and the rw bits, the last fixed word the revision.
  const auto base = number(out, ".error_logs.addresses.h", window + "BASE");
  CHECK(base == 0x100000);
  for (const auto *field :
       {"RECORD_HELD", "RECORD_OVERFLOW", "ALARM_ENABLE", "WAIT_WHEN_FULL", "RECORD_WRITE"})
    CHECK(base + number(out, ".error_logs.h", window + "FAULT_3_OFFSET") +
              number(out, ".error_logs.h", layout + field + "_OFFSET") ==
          0x1fffc4);
  CHECK(base + number(out, ".error_logs.h", window + "FAULT_3_OFFSET") +
            number(out, ".error_logs.h", layout + "REVISION_OFFSET") ==
        0x1fffdc);
  // Software takes a field of a word with the mask, then the shift.
  const auto id = number(out, ".error_logs.h", window + "FAULT_3_ID_VALUE");
  const auto part = [&](const std::string &field) {
    return (id & number(out, ".error_logs.h", layout + field + "_MASK")) >>
           number(out, ".error_logs.h", layout + field + "_SHIFT");
  };
  CHECK(part("LAYOUT") == number(out, ".error_logs.h", layout + "LAYOUT"));
  CHECK(part("LOG_INDEX") == 0);
  CHECK(part("LOGGER_INDEX") == 3);
  CHECK(part("CAPABILITY_NEXT_LOGGER") == 0);
  const auto map = nlohmann::json::parse(text(out, ".map.json"));
  const auto &banks = map.at("definition").at("log_registers").at("csr").at("banks");
  REQUIRE(banks.size() == 4);
  summary(map.at("definition").at("log_registers").at("csr"), 0, 0, {"next_logger"});
  for (unsigned k = 0; k < 4; ++k)
    summary(banks[k], 0, k,
            k < 3 ? std::vector<std::string>{"next_logger"} : std::vector<std::string>{});
  for (const auto &bank : banks) {
    const auto name = upper(bank.at("bank").get<std::string>());
    CHECK(number(out, ".error_logs.h", window + name + "_OFFSET") == bank.at("base"));
    std::uint64_t first = 0;
    for (const auto &field : bank.at("registers")) {
      const auto stem = layout + upper(field.at("name").get<std::string>());
      const auto lsb = field.at("lsb").get<unsigned>();
      const auto bits = (1ULL << field.at("width").get<unsigned>()) - 1;
      // The address of a field of this bank, in the window.
      CHECK(number(out, ".error_logs.h", window + name + "_OFFSET") +
                number(out, ".error_logs.h", stem + "_OFFSET") ==
            bank.at("base").get<unsigned>() + field.at("offset").get<unsigned>());
      CHECK(number(out, ".error_logs.h", stem + "_SHIFT") == lsb);
      // The mask covers the bits this bank's field has.
      CHECK((number(out, ".error_logs.h", stem + "_MASK") >> lsb & bits) == bits);
      // A whole bank has no macro per field.
      absent(out, ".error_logs.h",
             window + name + "_" + upper(field.at("name").get<std::string>()) + "_OFFSET");
      if (field.at("offset") == 0)
        first |= field.at("value").get<std::uint64_t>() << lsb;
    }
    CHECK(number(out, ".error_logs.h", window + name + "_ID_VALUE") == first);
  }
  // No revision is given: no bank states one, and the header is free of the generator version.
  CHECK(header.find("REVISION_VALUE") == std::string::npos);
  CHECK(header.find(std::string(version())) == std::string::npos);
  d.error_logging.logs[0].loggers.resize(1);
  out = headers(d);
  value(out, ".error_logs.h", window + "CSR_OFFSET", "(0x200U)");
  value(out, ".error_logs.h", window + "CSR_ID_VALUE", "(0x2000000U)");
  const auto single =
      nlohmann::json::parse(text(out, ".map.json")).at("definition").at("log_registers").at("csr");
  CHECK_FALSE(single.contains("banks"));
  summary(single, 0, 0, {});
}

TEST_CASE("C logger identification and revision values state each bank", "[c-header]") {
  auto d = logged();
  // Two loggers of the first log in the first window, the one logger of a second log in the next.
  d.error_logging.logs = {
      {"fault", {"ram"}, std::nullopt, false, {{.offset = 0x1000}, {.offset = 0x1040}}},
      {"more", {"hole"}, std::nullopt, false, {{.offset = 0x2000}}}};
  const std::string first = "NOCLOOM_HEADERS_CPU_CSR_WINDOW_0_";
  const std::string second = "NOCLOOM_HEADERS_CPU_CSR_WINDOW_1_";
  const auto parts = [](const ArtifactSet &out) {
    return nlohmann::json::parse(text(out, ".map.json"))
        .at("definition")
        .at("log_registers")
        .at("csr")
        .at("banks");
  };
  auto out = headers(d);
  // Layout 2 in bits 31:24, the log in 21:16, the logger in 12:8, a following logger in bit 3.
  value(out, ".error_logs.h", first + "CSR_OFFSET", "(0x0U)");
  value(out, ".error_logs.h", first + "CSR_ID_VALUE", "(0x2000008U)");
  value(out, ".error_logs.h", first + "FAULT_1_OFFSET", "(0x40U)");
  value(out, ".error_logs.h", first + "FAULT_1_ID_VALUE", "(0x2000100U)");
  value(out, ".error_logs.h", second + "MORE_OFFSET", "(0x0U)");
  value(out, ".error_logs.h", second + "MORE_ID_VALUE", "(0x2010000U)");
  CHECK(text(out, ".error_logs.h").find("REVISION_VALUE") == std::string::npos);
  CHECK(text(out, ".error_logs.h").find(std::string(version())) == std::string::npos);
  auto banks = parts(out);
  REQUIRE(banks.size() == 3);
  summary(banks[0], 0, 0, {"next_logger"});
  summary(banks[1], 0, 1, {});
  summary(banks[2], 1, 0, {});
  // A given revision sets bit 4 of each first word, and each bank states the revision word.
  d.error_logging.revision = 0xa5000001;
  out = headers(d);
  value(out, ".error_logs.h", first + "CSR_ID_VALUE", "(0x2000018U)");
  value(out, ".error_logs.h", first + "FAULT_1_ID_VALUE", "(0x2000110U)");
  value(out, ".error_logs.h", second + "MORE_ID_VALUE", "(0x2010010U)");
  value(out, ".error_logs.h", first + "CSR_REVISION_VALUE", "(0xa5000001U)");
  value(out, ".error_logs.h", first + "FAULT_1_REVISION_VALUE", "(0xa5000001U)");
  value(out, ".error_logs.h", second + "MORE_REVISION_VALUE", "(0xa5000001U)");
  CHECK(text(out, ".error_logs.h").find(std::string(version())) == std::string::npos);
  banks = parts(out);
  summary(banks[0], 0, 0, {"next_logger", "given_revision"});
  summary(banks[1], 0, 1, {"given_revision"});
  summary(banks[2], 1, 0, {"given_revision"});
}

TEST_CASE("C headers reject reserved macro and guard names", "[c-header]") {
  for (const auto *name : {"double__name", "trailing_"}) {
    for (const auto *object : {"module", "initiator", "target", "registers", "bank"}) {
      DYNAMIC_SECTION(object << ": " << name) {
        auto d = definition();
        d.initiators.resize(1);
        d.targets.resize(1);
        d.initiators[0].protocol = Protocol::Apb4;
        d.initiators[0].len_width.reset();
        d.targets[0].protocol = Protocol::Apb4;
        d.targets[0].len_width.reset();
        if (std::string_view(object) == "module")
          d.name = name;
        else if (std::string_view(object) == "initiator")
          d.initiators[0].name = name;
        else if (std::string_view(object) == "target")
          d.targets[0].name = name;
        else {
          d.targets.push_back({std::string_view(object) == "registers" ? name : "csr",
                               "main",
                               {{0x100000, 0x1000}}});
          d.targets[1].protocol = Protocol::Apb4;
          d.targets[1].data_width = 32;
          d.targets[1].attributes = TargetAttributes::amba();
          d.targets[1].log_registers = true;
          // A second logger uses the declared log name as its bank name.
          d.error_logging.logs = {{std::string_view(object) == "bank" ? name : "fault",
                                   {"t0"},
                                   std::nullopt,
                                   false,
                                   {{.offset = 0}, {.offset = 0x100}}}};
        }
        const auto plain = generate(d);
        REQUIRE(plain);
        const auto result = generate(d, {.c_header = true});
        REQUIRE_FALSE(result);
        REQUIRE(result.diagnostics().size() == 1);
        CHECK(result.diagnostics()[0].code == "NCL_C_HEADER_RESERVED");
        CHECK(result.diagnostics()[0].message.find("reserved in C11 or C++20") !=
              std::string::npos);
        CHECK(result.diagnostics()[0].message.find("__") != std::string::npos);
        CHECK(generate(d).value() == plain.value());
      }
    }
  }
}

TEST_CASE("C header windows follow smaller decode blocks", "[c-header]") {
  auto d = definition();
  d.targets[0].mappings = {{0, 0x30000}};
  d.targets[1].mappings = {{0x10800, 0x800}};
  d.targets[2].mappings = {{0x40000, 0x1000}};
  const auto out = headers(d);
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_0_BASE", "(0x0U)");
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_0_SIZE", "(0x10800U)");
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_1_BASE", "(0x11000U)");
  value(out, ".s0.addresses.h", "NOCLOOM_HEADERS_S0_T0_WINDOW_1_SIZE", "(0x1f000U)");
  CHECK(text(out, ".s0.addresses.h").find("\"default_target\":true") != std::string::npos);
}

TEST_CASE("C logger offsets pair with each reached window", "[c-header]") {
  auto d = logged();
  const auto out = headers(d);
  for (const auto *source : {"CPU", "DEBUG"}) {
    const auto prefix = std::string("NOCLOOM_HEADERS_") + source + "_CSR_WINDOW_";
    const auto file = source == std::string_view("CPU") ? ".cpu.addresses.h" : ".debug.addresses.h";
    value(out, file, prefix + "0_BASE", "(0x100000U)");
    value(out, file, prefix + "1_BASE", "(0x101000U)");
    value(out, ".error_logs.h", prefix + "0_CSR_OFFSET", "(0x0U)");
    value(out, ".error_logs.h", prefix + "1_FAULT_1_OFFSET", "(0x0U)");
    // The held bit of each bank, by the window's base, the bank's offset and the field's offset.
    const auto held = number(out, ".error_logs.h", "NOCLOOM_HEADERS_ERROR_LOG_RECORD_HELD_OFFSET");
    CHECK(number(out, file, prefix + "0_BASE") +
              number(out, ".error_logs.h", prefix + "0_CSR_OFFSET") + held ==
          0x100004);
    CHECK(number(out, file, prefix + "1_BASE") +
              number(out, ".error_logs.h", prefix + "1_FAULT_1_OFFSET") + held ==
          0x101004);
    CHECK(text(out, ".error_logs.h").find(prefix + "0_FAULT_1_") == std::string::npos);
    CHECK(text(out, ".error_logs.h").find(prefix + "1_CSR_") == std::string::npos);
  }
  CHECK(text(out, ".error_logs.h").find("NOCLOOM_HEADERS_NARROW_CSR_") == std::string::npos);
  CHECK(out == headers(d));
  std::ranges::reverse(d.initiators);
  CHECK(out == headers(d));
  d.targets[1].mappings.push_back({0x200000, 0x10000, 0x1000});
  const auto rejected = generate(d, {.c_header = true});
  REQUIRE_FALSE(rejected);
  CHECK(rejected.diagnostics()[0].code == "NCL_UNSUPPORTED_TOPOLOGY");
}

TEST_CASE("C logger macros of a cut bank place each field a window reaches", "[c-header]") {
  auto d = logged();
  // Eight bytes of another target lie in the second bank, over its words at 0x10 and 0x14: one
  // window ends before them and the next begins after them.
  d.targets[2].mappings = {{0x1007d0, 0x8}};
  d.error_logging.logs[0].loggers = {{.offset = 0x1000}, {.offset = 0x17c0}, {.offset = 0x2000}};
  d.error_logging.revision = 0x12345678;
  const auto out = headers(d);
  const auto map = nlohmann::json::parse(text(out, ".map.json"));
  const auto &cut = map.at("definition").at("log_registers").at("csr").at("banks").at(1);
  REQUIRE(cut.at("bank") == "fault_1");
  REQUIRE(cut.at("base") == 0x17c0);
  CHECK(cut.at("state_reachable") == true);
  CHECK(cut.at("unreachable_words") ==
        nlohmann::json{{"cpu", {0x10, 0x14}}, {"debug", {0x10, 0x14}}});
  CHECK(text(out, ".error_logs.h").find("No initiator can reach STATE") == std::string::npos);
  for (const auto *source : {"CPU", "DEBUG"}) {
    const auto prefix = std::string("NOCLOOM_HEADERS_") + source + "_CSR_WINDOW_";
    const auto file = source == std::string_view("CPU") ? ".cpu.addresses.h" : ".debug.addresses.h";
    value(out, file, prefix + "0_BASE", "(0x100000U)");
    value(out, file, prefix + "0_SIZE", "(0x7d0U)");
    value(out, file, prefix + "1_BASE", "(0x1007d8U)");
    // The whole banks on both sides.
    value(out, ".error_logs.h", prefix + "0_CSR_OFFSET", "(0x0U)");
    value(out, ".error_logs.h", prefix + "0_CSR_ID_VALUE", "(0x2000018U)");
    value(out, ".error_logs.h", prefix + "0_CSR_REVISION_VALUE", "(0x12345678U)");
    value(out, ".error_logs.h", prefix + "1_FAULT_2_OFFSET", "(0x828U)");
    value(out, ".error_logs.h", prefix + "1_FAULT_2_ID_VALUE", "(0x2000210U)");
    value(out, ".error_logs.h", prefix + "1_FAULT_2_REVISION_VALUE", "(0x12345678U)");
    // The cut bank: the first four words from the first window, the revision from the second.
    value(out, ".error_logs.h", prefix + "0_FAULT_1_LAYOUT_OFFSET", "(0x7c0U)");
    value(out, ".error_logs.h", prefix + "0_FAULT_1_LOGGER_INDEX_OFFSET", "(0x7c0U)");
    value(out, ".error_logs.h", prefix + "0_FAULT_1_RECORD_HELD_OFFSET", "(0x7c4U)");
    value(out, ".error_logs.h", prefix + "0_FAULT_1_WAIT_WHEN_FULL_OFFSET", "(0x7c4U)");
    value(out, ".error_logs.h", prefix + "0_FAULT_1_RECORD_ADDRESS_LOW_OFFSET", "(0x7c8U)");
    value(out, ".error_logs.h", prefix + "0_FAULT_1_RECORD_BYTES_OFFSET", "(0x7ccU)");
    value(out, ".error_logs.h", prefix + "1_FAULT_1_REVISION_OFFSET", "(0x4U)");
    CHECK(number(out, file, prefix + "0_BASE") +
              number(out, ".error_logs.h", prefix + "0_FAULT_1_RECORD_HELD_OFFSET") ==
          0x1007c4);
    CHECK(number(out, file, prefix + "1_BASE") +
              number(out, ".error_logs.h", prefix + "1_FAULT_1_REVISION_OFFSET") ==
          0x1007dc);
    // Every field of the bank: a macro in the window that reaches its word, and nowhere else.
    std::vector<std::string> covered;
    for (const auto &field : cut.at("registers")) {
      const auto name = upper(field.at("name").get<std::string>());
      const auto offset = field.at("offset").get<unsigned>();
      const auto low = prefix + "0_FAULT_1_" + name + "_OFFSET";
      const auto high = prefix + "1_FAULT_1_" + name + "_OFFSET";
      if (offset < 0x10) {
        value(out, ".error_logs.h", low, hex(0x7c0 + offset));
        absent(out, ".error_logs.h", high);
      } else if (offset < 0x18) {
        absent(out, ".error_logs.h", low);
        absent(out, ".error_logs.h", high);
        covered.push_back(name);
      } else {
        absent(out, ".error_logs.h", low);
        value(out, ".error_logs.h", high, hex(offset - 0x18));
      }
    }
    CHECK(covered == std::vector<std::string>{"RECORD_SOURCE_PATH", "RECORD_TARGET_PATH",
                                              "RECORD_REGION_INDEX"});
    // A cut bank states no offset of its own and no value of a word.
    for (const auto *window : {"0", "1"})
      for (const auto *macro : {"_OFFSET", "_ID_VALUE", "_REVISION_VALUE"})
        absent(out, ".error_logs.h", prefix + window + "_FAULT_1" + macro);
    // A window states nothing of a bank it reaches no byte of.
    CHECK(text(out, ".error_logs.h").find(prefix + "1_CSR_") == std::string::npos);
    CHECK(text(out, ".error_logs.h").find(prefix + "0_FAULT_2_") == std::string::npos);
  }
  CHECK(text(out, ".error_logs.h").find("NOCLOOM_HEADERS_NARROW_CSR_") == std::string::npos);
  summary(cut, 0, 1, {"next_logger", "given_revision"});
}

TEST_CASE("C log headers identify a bank whose state is hidden", "[c-header]") {
  auto d = logged();
  d.targets[2].mappings = {{0x100004, 4}};
  const auto out = headers(d);
  const auto map = nlohmann::json::parse(text(out, ".map.json"));
  const auto &bank = map.at("definition").at("log_registers").at("csr").at("banks").at(0);
  CHECK(bank.at("base") == 0x1000);
  CHECK(bank.at("state_reachable") == false);
  CHECK(bank.at("unreachable_words") == nlohmann::json{{"cpu", {4}}, {"debug", {4}}});
  CHECK(text(out, ".error_logs.h").find("/* No initiator can reach STATE of bank csr. */") !=
        std::string::npos);
}
