// Names that would reach the output as a keyword or as a second element of one name: the network
// module against emitted cell modules, Verilog-2001 keywords, the nets of the
// library cell models, error log banks and IP-XACT element names. Long names derived from
// 128-character names still generate. Names that differ only in letter case generate, and the
// file writer refuses them where the output directory holds their files as one.
#include "core/diagnostic.hpp"
#include "core/hardware.hpp"
#include "io/files.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <fstream>
#include <iterator>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <nocloom/ipxact.hpp>
#include <random>
#include <string>
#include <tuple>
#include <vector>

using namespace nocloom;

namespace {
std::string replaced(std::string text, const std::string &from, const std::string &to) {
  const auto at = text.find(from);
  REQUIRE(at != std::string::npos);
  return text.replace(at, from.size(), to);
}
std::string everywhere(std::string text, const std::string &from, const std::string &to) {
  for (auto at = text.find(from); at != std::string::npos; at = text.find(from, at + to.size()))
    text.replace(at, from.size(), to);
  return text;
}
// One APB initiator and target in a test-gated domain, with a reset synchronizer: the generated
// files hold the clock gate and the synchronizer cells.
const std::string base = R"(schema_version: 10
name: names
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}
initiators:
  - {name: control, clock_domain: main, protocol: apb4, data_width: 32}
targets:
  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x100000', size: '0x1000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
const std::string cells = "cells:\n"
                          "  clock_gate: {module: lib_icg, clock: CP, enable: E, test: TE, "
                          "output: Q}\n"
                          "  synchronizer: {module: lib_dff, stages: 1, clock: CK, data: D, "
                          "reset: RN, reset_active: low, output: Q}\n";
const std::string logged = R"(schema_version: 10
name: logged
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}
initiators:
  - {name: control, clock_domain: main, protocol: apb4, data_width: 32}
targets:
  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x100000', size: '0x1000'}]}
  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x300000', size: '0x1000'}]}
error_logging:
  registers: {name: csr, base: '0x200000', size: '0x100000', clock_domain: main}
  logs:
    - {name: errors, watch: [ram], loggers: [{offset: '0x2000'}, {offset: '0x3000'}]}
    - {name: faults, watch: [rom], loggers: [{offset: '0x4000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";

Definition parsed(const std::string &text) {
  auto p = config::parse(text, "names.yaml");
  REQUIRE(p);
  return p.value().definition;
}
// The text is rejected by validation and generation with code at field, located in the YAML.
void rejected(const std::string &text, const std::string &code, const std::string &field) {
  auto p = config::parse(text, "names.yaml");
  REQUIRE(p);
  const auto &d = p.value().definition;
  for (const auto &diagnostics : {nocloom::validate(d, {}, &p.value().sources).diagnostics(),
                                  nocloom::generate(d, {}, &p.value().sources).diagnostics()}) {
    REQUIRE_FALSE(diagnostics.empty());
    const auto hit = std::ranges::find_if(diagnostics, [&](const Diagnostic &x) {
      return x.code == code && x.field == field && x.location;
    });
    INFO(diagnostics.front().code << " [" << diagnostics.front().field
                                  << "]: " << diagnostics.front().message);
    CHECK(hit != diagnostics.end());
  }
  CHECK_FALSE(nocloom::generate(d));
}
const Diagnostic &only(const Status &status) {
  REQUIRE_FALSE(status);
  REQUIRE(status.diagnostics().size() == 1);
  return status.diagnostics().front();
}
bool has_file(const ArtifactSet &set, const std::string &path) {
  return std::ranges::find(set, path, &Artifact::path) != set.end();
}
} // namespace

TEST_CASE("The reserved cell modules are the cell files that generation writes",
          "[reserved-names]") {
  std::vector<std::string> written;
  for (const auto *reset : {"asynchronous", "synchronous"}) {
    const auto text =
        replaced(base, "language: verilog_2001}",
                 "language: verilog_2001, register_reset: " + std::string(reset) + "}");
    auto p = config::parse(text, "names.yaml");
    REQUIRE(p);
    const auto artifacts = nocloom::generate(p.value().definition, p.value().options);
    REQUIRE(artifacts);
    for (const auto &a : artifacts.value())
      if (a.path.ends_with(".v") && a.path != "nocloom_names.v")
        written.push_back(a.path.substr(0, a.path.size() - 2));
  }
  std::ranges::sort(written);
  written.erase(std::unique(written.begin(), written.end()), written.end());
  const std::vector<std::string> reserved(detail::cell_modules.begin(), detail::cell_modules.end());
  CHECK(written == reserved);
}

TEST_CASE("A network name that gives a cell module's name is rejected", "[reserved-names]") {
  for (const std::string name : {"clock_gate", "synchronizer", "synchronizer_sync_reset"}) {
    INFO(name);
    const auto text = replaced(base, "name: names", "name: " + name);
    if (name == "synchronizer_sync_reset") {
      auto input = config::parse(text, "names.yaml");
      REQUIRE(input);
      CHECK(nocloom::validate(input.value().definition));
      CHECK(nocloom::generate(input.value().definition));
      input.value().options.register_reset = RegisterReset::Synchronous;
      CHECK(only(nocloom::validate(input.value().definition, input.value().options)).code ==
            "NCL_NAME_OUTPUT_COLLISION");
      CHECK_FALSE(nocloom::generate(input.value().definition, input.value().options));
    } else {
      rejected(text, "NCL_NAME_OUTPUT_COLLISION", "name");
    }
  }
  // Names that only start like a cell module stay valid.
  for (const auto *name : {"clock_gates", "synchronizer_sync", "clock_gate_0"}) {
    INFO(name);
    const auto artifacts =
        nocloom::generate(parsed(replaced(base, "name: names", std::string("name: ") + name)));
    REQUIRE(artifacts);
    CHECK(has_file(artifacts.value(), "nocloom_" + std::string(name) + ".v"));
  }
}

TEST_CASE("A network name that differs from a cell module in letter case generates",
          "[reserved-names]") {
  using Case = std::tuple<std::string, std::string, RegisterReset>;
  for (const auto &[name, cell, reset] :
       {Case{"Clock_Gate", "nocloom_clock_gate.v", RegisterReset::Asynchronous},
        Case{"SYNCHRONIZER", "nocloom_synchronizer.v", RegisterReset::Asynchronous},
        Case{"Synchronizer_Sync_Reset", "nocloom_synchronizer_sync_reset.v",
             RegisterReset::Synchronous}}) {
    INFO(name);
    auto input = config::parse(replaced(base, "name: names", "name: " + name), "names.yaml");
    REQUIRE(input);
    auto &[d, options, sources] = input.value();
    options.register_reset = reset;
    CHECK(nocloom::validate(d, options, &sources));
    const auto artifacts = nocloom::generate(d, options, &sources);
    REQUIRE(artifacts);
    const auto top = "nocloom_" + name + ".v";
    CHECK(has_file(artifacts.value(), top));
    CHECK(has_file(artifacts.value(), cell));
    CHECK(io::detail::letter_case_twins(artifacts.value()) == std::pair{top, cell});
    // The two files hold two modules.
    for (const auto &a : artifacts.value())
      if (a.path == top || a.path == cell)
        CHECK(a.text.find("\nmodule " + a.path.substr(0, a.path.size() - 2) + " ") !=
              std::string::npos);
  }
}

TEST_CASE("Bank names that equal another bank stay rejected next to letter case",
          "[reserved-names]") {
  // A log named like the window or like the second bank of another log gives one bank name at two
  // addresses in the map. Names that differ from such a bank only in letter case are other names.
  rejected(replaced(logged, "name: faults", "name: errors_1"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[1].name");
  rejected(replaced(logged, "name: faults", "name: csr"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[1].name");
  for (const auto *name : {"Errors_1", "CSR"}) {
    INFO(name);
    CHECK(
        nocloom::generate(parsed(replaced(logged, "name: faults", std::string("name: ") + name))));
  }
}

TEST_CASE("Names follow the emitted Verilog-2001 language", "[reserved-names]") {
  for (const auto *word : {"uwire", "logic", "checker", "nettype", "always_ff", "unique0"}) {
    INFO(word);
    CHECK(detail::valid_name(word));
    CHECK(nocloom::generate(parsed(everywhere(base, "reset_n", word))));
    CHECK(nocloom::generate(parsed(replaced(base, "name: control", std::string("name: ") + word))));
    CHECK(nocloom::generate(parsed(replaced(base, "name: names", std::string("name: ") + word))));
  }
  rejected(everywhere(base, "reset_n", "module"), "NCL_NAME_INVALID", "reset_ports[0].name");
  rejected(replaced(base, "name: control", "name: module"), "NCL_NAME_INVALID",
           "initiators[0].name");
}

TEST_CASE("User flags accept labels without exposing bare RTL names", "[reserved-names]") {
  const auto declared = replaced(base, "address_width: 32\n",
                                 "address_width: 32\ntransport: {user_flags: [tagged, uwire]}\n");
  const auto config =
      replaced(declared, "protocol: apb4, data_width: 32}",
               "protocol: apb4, data_width: 32, user_flags: {tagged: 1, uwire: 0}}");
  CHECK(nocloom::generate(parsed(config)));
  rejected(everywhere(config, "tagged", "module"), "NCL_NAME_INVALID", "transport.user_flags[0]");
}

TEST_CASE("Library cell names keep apart from generated files and model nets", "[reserved-names]") {
  const auto bound = replaced(base, "clock_domains:\n", cells + "clock_domains:\n");
  const auto artifacts = nocloom::generate(parsed(bound));
  REQUIRE(artifacts);
  CHECK(has_file(artifacts.value(), "lib_icg.v"));
  CHECK(artifacts.value().size() == 8);
  // The behavioral models declare latched and stage beside the pins.
  rejected(replaced(bound, "enable: E", "enable: latched"), "NCL_NAME_OUTPUT_COLLISION",
           "cells.clock_gate.enable");
  rejected(replaced(bound, "data: D", "data: stage"), "NCL_NAME_OUTPUT_COLLISION",
           "cells.synchronizer.data");
  // Modules that differ only in letter case from another module are distinct modules and files.
  for (const auto &[from, to, other] :
       {std::array{"module: lib_icg", "Nocloom_Clock_Gate", "nocloom_clock_gate"},
        std::array{"module: lib_dff", "NOCLOOM_NAMES", "nocloom_names"},
        std::array{"module: lib_dff", "LIB_ICG", "lib_icg"}}) {
    INFO(to);
    const auto twins =
        nocloom::generate(parsed(replaced(bound, from, std::string("module: ") + to)));
    REQUIRE(twins);
    CHECK(twins.value().size() == 8);
    CHECK(has_file(twins.value(), std::string(to) + ".v"));
    CHECK(has_file(twins.value(), std::string(other) + ".v"));
    CHECK(io::detail::letter_case_twins(twins.value()));
  }
  rejected(replaced(bound, "module: lib_dff", "module: lib_icg"), "NCL_NAME_COLLISION",
           "cells.synchronizer.module");
  rejected(replaced(bound, "TE,", "module,"), "NCL_NAME_INVALID", "cells.clock_gate.test");
  // A pin may share a name with a net of the other model, and a module with the network.
  CHECK(nocloom::generate(parsed(replaced(bound, "enable: E", "enable: stage"))));
  CHECK(nocloom::generate(parsed(replaced(bound, "module: lib_icg", "module: Nocloom_Other"))));
}

TEST_CASE("The file writer keeps apart files that differ only in letter case", "[reserved-names]") {
  namespace fs = std::filesystem;
  using io::detail::write_artifacts;
  const ArtifactSet twins{{"top.v", "top"}, {"Cell.v", "upper"}, {"cell.v", "lower"}};
  CHECK(io::detail::letter_case_twins(twins) ==
        std::pair<std::string, std::string>{"Cell.v", "cell.v"});
  CHECK_FALSE(io::detail::letter_case_twins({{"top.v", ""}, {"cell.v", ""}}));
  // A new directory for each run, in the working directory of the test.
  const auto work = fs::absolute("letter-case-" + std::to_string(std::random_device{}()));
  REQUIRE(fs::create_directory(work));
  std::vector<std::optional<bool>> answers;
  std::size_t asked = 0;
  const auto probe = [&](const fs::path &directory) {
    CHECK(directory.parent_path() == work);
    REQUIRE(asked < answers.size());
    return answers[asked++];
  };
  const std::string one_file = "The output files Cell.v and cell.v differ only in letter case, and "
                               "the output directory holds such names as one file";
  const auto refused = [&](const fs::path &out, std::vector<std::optional<bool>> given,
                           const std::string &text) {
    answers = std::move(given);
    asked = 0;
    const auto result = write_artifacts(twins, out, probe);
    REQUIRE_FALSE(result);
    CHECK(asked == answers.size());
    const auto &d = result.diagnostics().front();
    CHECK(d.code == "NCL_IO_CASE");
    CHECK(d.notes.empty());
    CHECK(d.message == one_file + text);
  };
  const auto content = [](const fs::path &file) {
    std::ifstream in(file, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(in), {});
  };
  // This file system itself: where it holds the two names as one file, the second file is not
  // created, the first keeps its content and the files before it stay.
  std::ofstream(work / "Probe") << "first";
  const bool same = fs::exists(work / "pROBE");
  const auto written = [&](const fs::path &out, std::vector<std::optional<bool>> given) {
    answers = std::move(given);
    asked = 0;
    const auto result = write_artifacts(twins, out, probe);
    CHECK(asked == answers.size());
    CHECK(static_cast<bool>(result) == !same);
    CHECK(content(out / "Cell.v") == "upper");
    if (same) {
      CHECK(result.diagnostics().front().code == "NCL_IO_CASE");
      CHECK(result.diagnostics().front().message ==
            one_file + ". The files written before cell.v stay");
      CHECK(result.diagnostics().front().notes == std::vector<std::string>{"top.v", "Cell.v"});
    } else {
      CHECK(result.value().written_paths == std::vector<std::string>{"top.v", "Cell.v", "cell.v"});
      CHECK(content(out / "cell.v") == "lower");
    }
  };
  // The parent ignores letter case: nothing is created.
  refused(work / "ignored", {true}, "");
  CHECK_FALSE(fs::exists(work / "ignored"));
  // The parent shows it when it holds the new directory, which stays empty.
  refused(work / "late", {std::nullopt, true}, ". The new output directory is empty");
  CHECK(fs::is_empty(work / "late"));
  // A parent that keeps the names apart takes both files, before or after the directory exists.
  written(work / "kept", {false});
  written(work / "kept_late", {std::nullopt, false});
  // Where nothing shows it, the files are written as without twins, for any directory name.
  written(work / "2026", {std::nullopt, std::nullopt});
  written(work / "never/", {std::nullopt, std::nullopt});
  // An output directory that is present is reported as present, before any test.
  const auto untested = [](const fs::path &) -> std::optional<bool> {
    FAIL("The parent directory is tested");
    return std::nullopt;
  };
  for (const auto *name : {"123", "Present"}) {
    REQUIRE(fs::create_directory(work / name));
    const auto present = write_artifacts(twins, work / name, untested);
    REQUIRE_FALSE(present);
    CHECK(present.diagnostics().front().code == "NCL_IO_EXISTS");
    CHECK(fs::is_empty(work / name));
  }
  // Without such files the parent is not tested.
  CHECK(write_artifacts({{"top.v", "top"}, {"cell.v", "lower"}}, work / "plain", untested));
  // A file that is already in the new directory is never replaced.
  const auto planted = work / "planted";
  const auto kept = write_artifacts(twins, planted, [&](const fs::path &) -> std::optional<bool> {
    if (!fs::exists(planted))
      return std::nullopt;
    std::ofstream(planted / "top.v") << "other";
    return std::nullopt;
  });
  REQUIRE_FALSE(kept);
  CHECK(kept.diagnostics().front().code == "NCL_IO_WRITE");
  CHECK(kept.diagnostics().front().notes.empty());
  CHECK(content(planted / "top.v") == "other");
  // A twin that the directory finds under the name of a written sibling stops the writer there.
  // A file system that keeps the names apart shows it with a file planted under the twin's name.
  if (!same) {
    const auto folded = work / "folded";
    const auto stopped =
        write_artifacts(twins, folded, [&](const fs::path &) -> std::optional<bool> {
          if (fs::exists(folded))
            std::ofstream(folded / "cell.v") << "other";
          return std::nullopt;
        });
    REQUIRE_FALSE(stopped);
    CHECK(stopped.diagnostics().front().code == "NCL_IO_CASE");
    CHECK(stopped.diagnostics().front().message ==
          one_file + ". The files written before cell.v stay");
    CHECK(stopped.diagnostics().front().notes == std::vector<std::string>{"top.v", "Cell.v"});
    CHECK(content(folded / "cell.v") == "other");
  }
}

TEST_CASE("The parent directory shows how it treats letter case", "[reserved-names]") {
  namespace fs = std::filesystem;
  using io::detail::ignores_letter_case;
  const auto work = fs::absolute("letter-probe-" + std::to_string(std::random_device{}()));
  REQUIRE(fs::create_directories(work / "empty"));
  REQUIRE(fs::create_directory(work / "digits"));
  REQUIRE(fs::create_directory(work / "digits/2026"));
  // A parent without an entry with a letter, or one that cannot be read, shows nothing.
  CHECK_FALSE(ignores_letter_case(work / "empty/out"));
  CHECK_FALSE(ignores_letter_case(work / "digits/out"));
  CHECK_FALSE(ignores_letter_case(work / "digits/2026"));
  CHECK_FALSE(ignores_letter_case(work / "absent/out"));
  // This file system itself: a second file under the other letter case is a new file where it
  // keeps the names apart.
  std::ofstream(work / "Probe") << "first";
  const bool same = fs::exists(work / "pROBE");
  CHECK(ignores_letter_case(work / "out") == std::optional(same));
  // A directory that exists shows it by its own name, also in a parent that cannot be listed.
  CHECK(ignores_letter_case(work) == std::optional(same));
  REQUIRE(fs::create_directories(work / "locked/Out"));
  fs::permissions(work / "locked", fs::perms::owner_read, fs::perm_options::remove);
  std::error_code unread;
  fs::directory_iterator(work / "locked", unread);
  CHECK(ignores_letter_case(work / "locked/Out") == std::optional(same));
  if (unread)
    CHECK_FALSE(ignores_letter_case(work / "locked/New"));
  fs::permissions(work / "locked", fs::perms::owner_read, fs::perm_options::add);
  const ArtifactSet twins{{"Cell.v", "upper"}, {"cell.v", "lower"}};
  for (const auto &out : {work / "out", work / "empty/out"}) {
    const auto result = io::write_artifacts(twins, out);
    CHECK(static_cast<bool>(result) == !same);
    if (same) {
      CHECK(result.diagnostics().front().code == "NCL_IO_CASE");
      CHECK((!fs::exists(out) || fs::is_empty(out)));
    } else {
      CHECK(fs::file_size(out / "Cell.v") == 5);
      CHECK(fs::file_size(out / "cell.v") == 5);
    }
  }
  if (!same) {
    // A directory that lists both names keeps them apart.
    std::ofstream(work / "digits/Name") << "first";
    std::ofstream(work / "digits/nAME") << "second";
    CHECK(ignores_letter_case(work / "digits/out") == std::optional(false));
  }
}

TEST_CASE("The file writer locates letter case twins in the definition", "[reserved-names]") {
  // The writer names the pair of files in its message.
  const auto naming = [](const ArtifactSet &artifacts) {
    const auto pair = io::detail::letter_case_twins(artifacts);
    REQUIRE(pair);
    return Diagnostic{"NCL_IO_CASE",
                      "",
                      "The output files " + pair->first + " and " + pair->second +
                          " differ only in letter case",
                      {},
                      {}};
  };
  const Diagnostic unlocated{"NCL_IO_CASE", "", "text", {}, {}};
  const auto bound = replaced(base, "clock_domains:\n", cells + "clock_domains:\n");
  using Case = std::tuple<std::string, std::string, unsigned, unsigned>;
  for (const auto &[text, field, line, column] :
       {Case{replaced(base, "name: names", "name: Clock_Gate"), "name", 2, 7},
        Case{replaced(bound, "module: lib_icg", "module: Nocloom_Clock_Gate"),
             "cells.clock_gate.module", 7, 24},
        Case{replaced(bound, "module: lib_dff", "module: NOCLOOM_NAMES"),
             "cells.synchronizer.module", 8, 26},
        Case{replaced(bound, "module: lib_dff", "module: LIB_ICG"), "cells.synchronizer.module", 8,
             26}}) {
    INFO(field);
    const auto input = config::parse(text, "names.yaml");
    REQUIRE(input);
    const auto artifacts = nocloom::generate(input.value().definition);
    REQUIRE(artifacts);
    const auto d =
        io::detail::located({naming(artifacts.value())}, artifacts.value(), input.value()).front();
    CHECK(d.field == field);
    REQUIRE(d.location);
    CHECK(d.location->label == "names.yaml");
    CHECK(d.location->line == line);
    CHECK(d.location->column == column);
    // Other diagnostics of the writer keep their fields.
    const Diagnostic other{"NCL_IO_WRITE", "", "text", {}, {}};
    const auto kept = io::detail::located({other}, artifacts.value(), input.value()).front();
    CHECK(kept.field.empty());
    CHECK_FALSE(kept.location);
    // A message that names no pair of these files keeps the diagnostic as it is.
    CHECK(io::detail::located({unlocated}, artifacts.value(), input.value()).front().field.empty());
  }
  // Without twins the diagnostic keeps its field.
  const auto input = config::parse(base, "names.yaml");
  REQUIRE(input);
  const auto artifacts = nocloom::generate(input.value().definition);
  REQUIRE(artifacts);
  CHECK(io::detail::located({unlocated}, artifacts.value(), input.value()).front().field.empty());
}

TEST_CASE("Error log banks take distinct names", "[reserved-names]") {
  const auto d = nocloom::generate(parsed(logged));
  REQUIRE(d);
  // errors_1 is the second logger of errors: a log of that name would share its bank name.
  rejected(replaced(logged, "name: faults", "name: errors_1"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[1].name");
  // The first log's first bank takes the window's name.
  rejected(replaced(logged, "name: faults", "name: csr"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[1].name");
  const auto chained =
      replaced(replaced(logged, "name: faults", "name: x"), "loggers: [{offset: '0x4000'}]",
               "loggers: [{offset: '0x4000'}, {offset: '0x5000'}]");
  REQUIRE(nocloom::generate(parsed(chained)));
  rejected(replaced(chained, "name: x,", "name: errors_1,"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[1].name");
  // The first log's own name names no bank: its first bank is the window's.
  CHECK(nocloom::generate(parsed(replaced(chained, "name: errors,", "name: x_1,"))));
  // A later logger's bank <log>_<n> meets the bank of an earlier log of that name.
  const auto third =
      replaced(replaced(chained, "  - {name: rom,",
                        "  - {name: dev, clock_domain: main, protocol: apb4, data_width: 32, "
                        "attributes: amba,\n     mappings: [{base: '0x400000', size: '0x1000'}]}\n"
                        "  - {name: rom,"),
               "    - {name: x,",
               "    - {name: y_1, watch: [dev], loggers: [{offset: '0x6000'}]}\n"
               "    - {name: x,");
  REQUIRE(nocloom::generate(parsed(third)));
  rejected(replaced(third, "name: x,", "name: y,"), "NCL_NAME_OUTPUT_COLLISION",
           "error_logging.logs[2].loggers[1]");
}

TEST_CASE("Error log names are distinct before planning", "[reserved-names]") {
  REQUIRE(nocloom::generate(parsed(logged)));
  const auto text = replaced(logged, "name: faults", "name: errors");
  const auto result = config::parse(text, "names.yaml");
  REQUIRE(result);
  const auto &input = result.value();
  const auto check = [](const auto &outcome) {
    REQUIRE_FALSE(outcome);
    REQUIRE(outcome.diagnostics().size() == 1);
    const auto &diagnostic = outcome.diagnostics().front();
    CHECK(diagnostic.code == "NCL_NAME_OUTPUT_COLLISION");
    CHECK(diagnostic.field == "error_logging.logs[1].name");
    CHECK(diagnostic.message.find("errors") != std::string::npos);
    REQUIRE(diagnostic.location);
    CHECK(diagnostic.location->label == "names.yaml");
    CHECK(diagnostic.location->line == 19);
    CHECK(diagnostic.location->column == 14);
  };
  check(nocloom::make_plan(input.definition, &input.sources));
  check(nocloom::validate(input.definition, {}, &input.sources));
  check(nocloom::generate(input.definition, {}, &input.sources));
}

TEST_CASE("Names derived from 128-character names generate", "[reserved-names]") {
  const std::string log(128, 'e'), window(128, 'w'), endpoint(127, 'i');
  auto text = replaced(logged, "name: errors,", "name: " + log + ",");
  text = replaced(text, "name: faults", "name: f" + log.substr(1));
  text = replaced(text, "name: csr", "name: " + window);
  text = replaced(text, "name: control", "name: " + endpoint);
  text = replaced(text, "name: logged", "name: " + std::string(128, 'n'));
  const auto artifacts = nocloom::generate(parsed(text));
  REQUIRE(artifacts);
  const auto map = std::ranges::find_if(
      artifacts.value(), [](const Artifact &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != artifacts.value().end());
  CHECK(map->text.find("\"" + log + "_1\"") != std::string::npos);
  // The header of each initiator's windows holds two names in its file name: over 255 bytes is
  // more than common file systems take.
  GenerationOptions options;
  options.c_header = true;
  const auto headers = nocloom::generate(parsed(text), options);
  REQUIRE_FALSE(headers);
  CHECK(headers.diagnostics().front().code == "NCL_C_HEADER_NAME");
  const auto shorter = nocloom::generate(
      parsed(replaced(text, "name: " + endpoint, "name: " + std::string(100, 'i'))), options);
  CHECK(shorter);
}

TEST_CASE("IP-XACT element names stay distinct", "[reserved-names]") {
  GenerationOptions ipxact;
  ipxact.ipxact = true;
  // An initiator and a target of one name would be two bus interfaces of that name.
  const auto shared = replaced(base, "name: ram", "name: control");
  REQUIRE(nocloom::generate(parsed(shared)));
  const auto status = nocloom::validate(parsed(shared), ipxact);
  CHECK(only(status).code == "NCL_IPXACT_NAME");
  CHECK(only(status).field == "targets[0].name");
  CHECK_FALSE(nocloom::generate(parsed(shared), ipxact));
  const auto plain = nocloom::generate(parsed(shared));
  REQUIRE(plain);
  const auto added = nocloom::add_ipxact(parsed(shared), plain.value());
  REQUIRE_FALSE(added);
  CHECK(added.diagnostics().front().code == "NCL_IPXACT_NAME");
  CHECK(nocloom::generate(parsed(base), ipxact));
  // The remap state of high and rom is named high_and_rom (selectors in name order), the name of
  // the state of a selector high_and_rom.
  const std::string modes = R"(schema_version: 10
name: modes
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
address_inputs:
  - {name: boot, width: 1, clock_domain: main}
  - {name: bank, width: 2, clock_domain: main}
address_selectors:
  - {name: rom, equals: {boot: 1}}
  - {name: high, equals: {bank: 2}}
  - {name: both, equals: {boot: 1, bank: 2}}
initiators:
  - {name: cpu, protocol: apb4, data_width: 32, clock_domain: main}
targets:
  - {name: dev, protocol: apb4, data_width: 32, clock_domain: main, attributes: amba,
     mappings: [{base: '0x4000', size: '0x1000', when: {rom: true, high: true}},
                {base: '0x6000', size: '0x1000', when: {both: true}}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
  REQUIRE(nocloom::generate(parsed(modes), ipxact));
  const auto clash = replaced(replaced(modes, "name: both,", "name: high_and_rom,"),
                              "when: {both: true}", "when: {high_and_rom: true}");
  REQUIRE(nocloom::generate(parsed(clash)));
  const auto remap = nocloom::validate(parsed(clash), ipxact);
  CHECK(only(remap).code == "NCL_IPXACT_NAME");
  CHECK(only(remap).field == "targets[0].mappings[1].when");
}

TEST_CASE("Output diagnostics use definition locations", "[reserved-names]") {
  GenerationOptions options;
  std::string text, code, field;
  unsigned line = 0, column = 0;
  SECTION("C header collision at the output block") {
    options.c_header = true;
    text = replaced(base, "targets:\n",
                    "  - {name: Control, clock_domain: main, protocol: apb4, data_width: 32}\n"
                    "targets:\n");
    code = "NCL_C_HEADER_NAME";
    field = "output.c_header";
    line = 15;
    column = 9;
  }
  SECTION("IP-XACT collision at the target name") {
    options.ipxact = true;
    text = replaced(base, "name: ram", "name: control");
    code = "NCL_IPXACT_NAME";
    field = "targets[0].name";
    line = 11;
    column = 12;
  }
  const auto loaded = config::parse(text, "outputs.yaml");
  REQUIRE(loaded);
  const auto &input = loaded.value();
  REQUIRE(nocloom::generate(input.definition));
  const auto check = [&](const auto &result, bool located) {
    REQUIRE_FALSE(result);
    REQUIRE(result.diagnostics().size() == 1);
    const auto &diagnostic = result.diagnostics().front();
    CHECK(diagnostic.code == code);
    CHECK(diagnostic.field == field);
    REQUIRE(diagnostic.location.has_value() == located);
    if (located) {
      CHECK(diagnostic.location->label == "outputs.yaml");
      CHECK(diagnostic.location->line == line);
      CHECK(diagnostic.location->column == column);
    }
  };
  check(nocloom::validate(input.definition, options, &input.sources), true);
  check(nocloom::generate(input.definition, options, &input.sources), true);
  check(nocloom::validate(input.definition, options), false);
  check(nocloom::generate(input.definition, options), false);
}
