#include "diff.hpp"
#include "nocloom/io.hpp"
#include <CLI/CLI.hpp>
#include <exception>
#include <iostream>
#include <new>

namespace {
int report(const std::vector<nocloom::Diagnostic> &diagnostics) {
  int code = 1;
  for (const auto &d : diagnostics) {
    if (d.code.starts_with("NCL_IO_"))
      code = 3;
    if (d.location)
      std::cerr << d.location->label << ':' << d.location->line << ':' << d.location->column
                << ": ";
    std::cerr << d.code << " [" << d.field << "]: " << d.message << '\n';
    // An I/O failure lists the files it already wrote; other notes explain the diagnostic.
    for (const auto &note : d.notes)
      std::cerr << (d.code.starts_with("NCL_IO_") ? "output created: " : "note: ") << note << '\n';
  }
  return code;
}
int run(int argc, char **argv) {
  CLI::App app{"NocLoom: weave your interconnect"};
  app.set_version_flag("--version", std::string(nocloom::version()));
  app.require_subcommand(1);
  std::string input;
  std::string output;
  std::string target = "verilog";
  bool ipxact = false;
  bool address_table = false;
  bool cdc_constraints = false;
  bool c_header = false;
  nocloom::ValidationOptions limits;
  auto *validate = app.add_subcommand("validate", "Validate an interconnect definition");
  validate->add_option("input", input, "YAML definition")->required();
  auto *generate = app.add_subcommand("generate", "Generate Verilog-2001 and metadata");
  generate->add_option("input", input, "YAML definition")->required();
  generate->add_option("--out-dir", output, "New output directory (parent must exist)")->required();
  for (auto *command : {validate, generate}) {
    command->add_option("--target", target, "Backend: verilog");
    command->add_flag("--ipxact", ipxact, "Request an IEEE 1685-2009 IP-XACT component");
    command->add_flag("--address-table", address_table,
                      "Request each initiator's address windows as JSON and CSV");
    command->add_flag("--cdc-constraints", cdc_constraints,
                      "Request CDC timing exceptions and a synthesis preserve list");
    command->add_flag("--c-header", c_header,
                      "Request C headers for address windows and error log registers");
    command->add_option("--cycle-limit", limits.cycle_limit,
                        "Route cycles reported per direction, 1 to 1024 (default 4)");
    command->add_option("--contributor-limit", limits.contributor_limit,
                        "Routes listed per wait of a route cycle, 0 for all (default 8)");
  }
  std::string before;
  std::string after;
  bool full = false;
  auto *diff = app.add_subcommand(
      "diff", "Compare two definitions by meaning (exit 0: none, 4: differences)");
  diff->add_option("old", before, "YAML definition or map.json")->required();
  diff->add_option("new", after, "YAML definition or map.json")->required();
  diff->add_flag("--full", full,
                 "Compare every map field, not only the definition, ports, module and resource "
                 "names");
  try {
    app.parse(argc, argv);
  } catch (const CLI::ParseError &error) {
    const int status = app.exit(error);
    return status == 0 ? 0 : 2;
  }
  if (target != "verilog") {
    std::cerr << "NCL_UNSUPPORTED_BACKEND [output.backend]: Only Verilog-2001 is implemented\n";
    return 1;
  }
  if (*diff) {
    auto old_map = nocloom::cli::load_map(before);
    if (!old_map)
      return report(old_map.diagnostics());
    auto new_map = nocloom::cli::load_map(after);
    if (!new_map)
      return report(new_map.diagnostics());
    const auto lines = nocloom::cli::map_diff(old_map.value(), new_map.value(),
                                              full ? nocloom::cli::DiffScope::Full
                                                   : nocloom::cli::DiffScope::Interface);
    for (const auto &line : lines)
      std::cout << line << '\n';
    if (lines.empty())
      std::cout << "No semantic differences\n";
    return lines.empty() ? 0 : 4;
  }
  auto parsed = nocloom::io::load_definition(input);
  if (!parsed)
    return report(parsed.diagnostics());
  const auto &definition = parsed.value();
  auto options = definition.options;
  options.validation = limits;
  options.cdc_constraints = cdc_constraints;
  options.c_header = c_header;
  options.address_table = address_table;
  options.ipxact = ipxact;
  if (*validate) {
    auto status = nocloom::validate(definition.definition, options, &definition.sources);
    if (!status)
      return report(status.diagnostics());
    std::cout << "Definition is valid\n";
    return 0;
  }
  auto artifacts = nocloom::generate(definition.definition, options, &definition.sources);
  if (!artifacts)
    return report(artifacts.diagnostics());
  auto written = nocloom::io::write_artifacts(artifacts.value(), output);
  if (!written)
    return report(written.diagnostics());
  std::cout << "Generated " << written.value().written_paths.size() << " files\n";
  // Reported, not rejected: the dependency graph's check (docs/outputs.md#dependency-graph).
  for (const auto &artifact : artifacts.value())
    if (artifact.path.ends_with(".dependency.json") &&
        artifact.text.find("\"status\": \"cycle\"") != std::string::npos)
      std::cout << "Dependency cycle reported in " << artifact.path << "\n";
  return 0;
}
} // namespace
// A failure that reaches here was not reported as a diagnostic: report it and exit nonzero.
int main(int argc, char **argv) {
  try {
    return run(argc, argv);
  } catch (const std::bad_alloc &) {
    std::cerr << "NCL_RESOURCE_LIMIT []: Out of memory\n";
  } catch (const std::exception &error) {
    std::cerr << "NCL_INTERNAL_PLAN []: Unexpected failure: " << error.what() << '\n';
  } catch (...) {
    std::cerr << "NCL_INTERNAL_PLAN []: Unexpected failure\n";
  }
  return 1;
}
