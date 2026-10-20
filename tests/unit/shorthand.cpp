// YAML shorthand: each short file under tests/shorthand (and the shorthand
// example) must parse to the Definition of its written-out partner, field by
// field and in order, and generate the same artifacts byte for byte. Each
// misuse names its code, field and source position.
#include "diff.hpp"
#include <catch2/catch_test_macros.hpp>
#include <filesystem>
#include <fstream>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <sstream>
#include <string>

namespace {
std::string read(const std::string &relative) {
  std::ifstream input(std::string(NOCLOOM_SOURCE_DIR) + "/" + relative, std::ios::binary);
  REQUIRE(input);
  std::ostringstream text;
  text << input.rdbuf();
  return text.str();
}
nocloom::ParsedDefinition parsed(const std::string &text) {
  auto result = nocloom::config::parse(text, "input.yaml");
  if (!result)
    FAIL(result.diagnostics().front().code + " [" + result.diagnostics().front().field +
         "]: " + result.diagnostics().front().message);
  return result.value();
}
void same(const std::string &short_file, const std::string &full_file) {
  INFO(short_file << " against " << full_file);
  const auto brief = parsed(read(short_file));
  const auto full = parsed(read(full_file));
  const auto &a = brief.definition;
  const auto &b = full.definition;
  // Section by section first, so a failure names the section.
  CHECK(a.initiators == b.initiators);
  CHECK(a.targets == b.targets);
  CHECK(a.connectivity == b.connectivity);
  CHECK(a.topology == b.topology);
  REQUIRE(a == b);
  const auto left = nocloom::generate(a, brief.options, &brief.sources);
  const auto right = nocloom::generate(b, full.options, &full.sources);
  REQUIRE(left);
  REQUIRE(right);
  REQUIRE(left.value().size() >= 3);
  REQUIRE(left.value() == right.value());
}
// The first occurrence of `from` replaced by `to`.
std::string edit(std::string text, const std::string &from, const std::string &to) {
  const auto at = text.find(from);
  REQUIRE(at != std::string::npos);
  return text.replace(at, from.size(), to);
}
// Line and column (1-based) of `needle` plus `offset` characters.
std::pair<unsigned, unsigned> where(const std::string &text, const std::string &needle,
                                    std::size_t offset = 0) {
  auto at = text.find(needle);
  REQUIRE(at != std::string::npos);
  at += offset;
  const auto line_start = text.rfind('\n', at);
  const auto line = static_cast<unsigned>(std::count(text.begin(), text.begin() + at, '\n')) + 1;
  const auto column =
      static_cast<unsigned>(line_start == std::string::npos ? at + 1 : at - line_start);
  return {line, column};
}
void located(const nocloom::Diagnostic &d, const std::string &code, const std::string &field,
             std::pair<unsigned, unsigned> position) {
  INFO(d.code << " [" << d.field << "]: " << d.message);
  CHECK(d.code == code);
  CHECK(d.field == field);
  REQUIRE(d.location);
  CHECK(d.location->label == "input.yaml");
  CHECK(std::pair{d.location->line, d.location->column} == position);
}
void rejected(const std::string &text, const std::string &code, const std::string &field,
              std::pair<unsigned, unsigned> position) {
  const auto result = nocloom::config::parse(text, "input.yaml");
  REQUIRE_FALSE(result);
  located(result.diagnostics().front(), code, field, position);
}
void invalid(const std::string &text, const std::string &code, const std::string &field,
             std::pair<unsigned, unsigned> position) {
  const auto input = parsed(text);
  const auto status = nocloom::validate(input.definition, input.options, &input.sources);
  REQUIRE_FALSE(status);
  located(status.diagnostics().front(), code, field, position);
}
} // namespace

TEST_CASE("shorthand expands to the written-out definition and bytes", "[config-shorthand]") {
  for (const auto *pair : {"templates", "groups", "mirror", "node_pipes"})
    same(std::string("tests/shorthand/") + pair + "_short.yaml",
         std::string("tests/shorthand/") + pair + "_full.yaml");
  same("examples/scale_axi_12_short.yaml", "examples/scale_axi_12.yaml");
}

TEST_CASE("endpoint templates reject ambiguous or unused fields", "[config-shorthand]") {
  const auto base = read("tests/shorthand/templates_short.yaml");
  auto text = edit(base, "use: cpu, max", "use: cpux, max");
  rejected(text, "NCL_TEMPLATE_REFERENCE", "initiators[1].use", where(text, "cpux"));
  text = edit(base, "use: [cpu, staged]", "use: [cpu, memory]");
  rejected(text, "NCL_TEMPLATE_REFERENCE", "initiators[0].use[1]", where(text, "memory]"));
  text = edit(base, "use: cpu, max_outstanding: 2", "use: cpu, data_width: 32");
  rejected(text, "NCL_TEMPLATE_CONFLICT", "initiators[1].data_width",
           where(text, "use: cpu, data_width: 32", 22));
  text = edit(base, "staged: {pipes:", "staged: {len_width: 8, pipes:");
  rejected(text, "NCL_TEMPLATE_CONFLICT", "initiators[0].len_width", where(text, "staged]"));
  text = edit(base, "use: [cpu, staged]", "use: [cpu, cpu]");
  rejected(text, "NCL_TEMPLATE_CONFLICT", "initiators[0].use[1]", where(text, "[cpu, cpu]", 6));
  text = edit(base, "cpu: {clock_domain", "cpu: {name: x, clock_domain");
  rejected(text, "NCL_TEMPLATE_FIELD", "templates.initiators.cpu.name", where(text, "name: x"));
  text = edit(base, "cpu: {clock_domain", "cpu: {colour: x, clock_domain");
  rejected(text, "NCL_SCHEMA_UNKNOWN_FIELD", "templates.initiators.cpu.colour",
           where(text, "colour"));
  text = edit(base, "  targets:\n", "  targets:\n    spare: {access: read_only}\n");
  rejected(text, "NCL_TEMPLATE_UNUSED", "templates.targets.spare",
           where(text, "{access: read_only}"));
  // A validation diagnostic of a template field points at the template.
  text = edit(base, "data_width: 64, len_width: 8, attributes",
              "data_width: 48, len_width: 8, attributes");
  const auto input = parsed(text);
  const auto status = nocloom::validate(input.definition, input.options, &input.sources);
  REQUIRE_FALSE(status);
  CHECK(status.diagnostics().front().field == "targets[0].data_width");
  REQUIRE(status.diagnostics().front().location);
  CHECK(status.diagnostics().front().location->line == where(text, "48").first);
}

TEST_CASE("connectivity groups reject unknown, colliding and unused names", "[config-shorthand]") {
  const auto base = read("tests/shorthand/groups_short.yaml");
  auto text = edit(base, "[cpu0, cpu1]", "[cpu0, cpu9]");
  rejected(text, "NCL_GROUP_REFERENCE", "groups.initiators.cores[1]", where(text, "cpu9"));
  text = edit(base, "[cpu0, cpu1]", "[cpu0, cpu0]");
  rejected(text, "NCL_GROUP_DUPLICATE", "groups.initiators.cores[1]",
           where(text, "[cpu0, cpu0]", 7));
  text = edit(base, "    memory: [ram, rom]\n", "    memory: [ram, rom]\n    boot: [rom]\n");
  rejected(text, "NCL_GROUP_NAME", "groups.targets.boot", where(text, "boot: [rom]"));
  text = edit(base, "    memory: [ram, rom]\n", "    memory: [ram, rom]\n    spare: [ram]\n");
  rejected(text, "NCL_GROUP_UNUSED", "groups.targets.spare", where(text, "spare: [ram]"));
  text = edit(base, "[peripherals, boot]", "[peripherals, boat]");
  rejected(text, "NCL_CONNECTIVITY_REFERENCE", "connectivity.dma.all_except[1]",
           where(text, "boat"));
  text = edit(base, "dbg: [peripherals, memory]\n", "dbg: [peripherals, memory]\n  cpu0: [ram]\n");
  rejected(text, "NCL_CONNECTIVITY_OVERLAP", "connectivity.cpu0", where(text, "cpu0: [ram]"));
  text = edit(base, "dbg: [peripherals, memory]", "dbg: [peripherals, cores]");
  rejected(text, "NCL_CONNECTIVITY_REFERENCE", "connectivity.dbg[1]", where(text, "cores]"));
  // An expanded pair keeps the position of the name that produced it.
  text = edit(base, "dbg: [peripherals, memory]", "dbg: [peripherals, uart]");
  text = edit(text, "    memory: [ram, rom]\n", "");
  invalid(text, "NCL_CONNECTIVITY_DUPLICATE", "connectivity.dbg[2]", where(text, "uart]"));
}

TEST_CASE("mirrored response graphs reject bad renames and overrides", "[config-shorthand]") {
  const auto base = read("tests/shorthand/mirror_short.yaml");
  auto text = edit(base, "{from: _req, to: _rsp}", "{from: _rq, to: _rsp}");
  rejected(text, "NCL_MIRROR_RENAME", "topology.response.mirror.rename_suffix",
           where(text, "{from: _rq"));
  text = edit(base, "{from: periph_rsp, to: core_rsp", "{from: periph_rsp, to: cpu_rsp");
  rejected(text, "NCL_MIRROR_OVERRIDE", "topology.response.mirror.link_overrides[0]",
           where(text, "{from: periph_rsp"));
  text = edit(base, "core_rsp, sender_pipeline: {forward: true}}",
              "core_rsp, sender_pipeline: {forward: true, backward: true}}");
  rejected(text, "NCL_MIRROR_OVERRIDE", "topology.response.mirror.link_overrides[0]",
           where(text, "{from: periph_rsp"));
  text = edit(base, "rename_suffix:", "renames:");
  rejected(text, "NCL_SCHEMA_UNKNOWN_FIELD", "topology.response.mirror.renames",
           where(text, "renames"));
}

TEST_CASE("node pipe defaults reject conflicts and defaults no link takes", "[config-shorthand]") {
  const auto base = read("tests/shorthand/node_pipes_short.yaml");
  auto text =
      edit(base, "- {name: leaf0}", "- {name: leaf0, input_links: {pipeline: {forward: true}}}");
  rejected(text, "NCL_NODE_PIPE_CONFLICT", "topology.request.links[2]",
           where(text, "{from: hub, to: leaf0}"));
  text = edit(base, "- {name: leaf1}", "- {name: leaf1, input_links: {pipeline: {forward: true}}}");
  rejected(text, "NCL_NODE_PIPE_UNUSED", "topology.request.nodes[4].input_links.pipeline",
           where(text, "{pipeline: {forward: true}}}", 11));
}

TEST_CASE("diff compares maps by element name", "[config-shorthand]") {
  using nlohmann::json;
  using nocloom::cli::DiffScope;
  const auto before = json::parse(R"({"format_version": 1, "generator": "a", "module": "m",
    "definition": {"initiators": ["s0", "s1"], "connectivity": [{"source": "s0", "target": "t0"}],
                   "bits": [0, 0, 1], "targets": [{"name": "t0", "end": "4096"}]},
    "ports": [{"name": "p0", "width": 1}],
    "network": {"resources": [{"name": "r0", "role": "x"}], "storage": [{"x": 1}]}})");
  CHECK(nocloom::cli::map_diff(before, before, DiffScope::Interface).empty());
  auto after = before;
  after["generator"] = "b";
  after["network"]["storage"][0]["x"] = 2;
  CHECK(nocloom::cli::map_diff(before, after, DiffScope::Interface).empty());
  CHECK(nocloom::cli::map_diff(before, after, DiffScope::Full) ==
        std::vector<std::string>{"~ network.storage[0].x: 1 -> 2"});
  after = before;
  after["definition"]["initiators"] = json::array({"s1", "s0", "s2"});
  after["definition"]["connectivity"][0]["target"] = "t1";
  after["definition"]["bits"][1] = 1;
  after["definition"]["targets"][0]["end"] = "8192";
  after["ports"][0]["width"] = 2;
  after["network"]["resources"][0]["role"] = "y";
  after["network"]["resources"].push_back({{"name", "r1"}});
  CHECK(nocloom::cli::map_diff(before, after, DiffScope::Interface) ==
        std::vector<std::string>{
            "~ definition.bits[1]: 0 -> 1",
            "- definition.connectivity[s0->t0]: {\"source\":\"s0\",\"target\":\"t0\"}",
            "+ definition.connectivity[s0->t1]: {\"source\":\"s0\",\"target\":\"t1\"}",
            "+ definition.initiators[s2]: \"s2\"", "~ definition.initiators: order",
            "~ definition.targets[t0].end: \"4096\" -> \"8192\"", "+ network.resources[r1]: \"r1\"",
            "~ ports[p0].width: 1 -> 2"});
}

TEST_CASE("diff reads YAML definitions and maps only", "[config-shorthand]") {
  // Written into the unit working directory of the build tree.
  const auto rejected_input = [](const std::string &name, const std::string &content,
                                 const std::string &code) {
    INFO(name);
    if (!content.empty())
      std::ofstream(name, std::ios::binary) << content;
    const auto result = nocloom::cli::load_map(name);
    if (!content.empty()) {
      const auto work = std::filesystem::canonical(std::filesystem::current_path());
      REQUIRE_FALSE(std::filesystem::is_symlink(name));
      REQUIRE(std::filesystem::weakly_canonical(work / name).parent_path() == work);
      std::filesystem::remove(name);
    }
    REQUIRE_FALSE(result);
    CHECK(result.diagnostics().front().code == code);
  };
  rejected_input("nocloom-diff-input.txt", "", "NCL_INPUT_FORMAT");
  rejected_input("nocloom-diff-absent.json", "", "NCL_IO_READ");
  rejected_input("nocloom-diff-text.json", "not json", "NCL_DIFF_MAP");
  rejected_input("nocloom-diff-bundle.json",
                 R"({"format_version": 1, "definition": {}, "files": []})", "NCL_DIFF_MAP");
  // A map has the size and nesting limits of a YAML definition.
  using nocloom::config::max_depth, nocloom::config::max_input_bytes;
  rejected_input("nocloom-diff-nested.json",
                 std::string(max_depth, '[') + std::string(max_depth, ']'), "NCL_DIFF_MAP");
  rejected_input("nocloom-diff-deep.json",
                 std::string(max_depth + 1, '[') + std::string(max_depth + 1, ']'),
                 "NCL_RESOURCE_LIMIT");
  rejected_input("nocloom-diff-unclosed.json", std::string(60000, '['), "NCL_RESOURCE_LIMIT");
  rejected_input(
      "nocloom-diff-objects.json",
      [] {
        std::string text;
        for (unsigned i = 0; i < 60000; ++i)
          text += R"({"a":)";
        return text;
      }(),
      "NCL_RESOURCE_LIMIT");
  rejected_input("nocloom-diff-large.json", std::string(max_input_bytes + 1, ' '),
                 "NCL_RESOURCE_LIMIT");
  const auto map = nocloom::cli::load_map(std::string(NOCLOOM_SOURCE_DIR) +
                                          "/tests/shorthand/groups_short.yaml");
  REQUIRE(map);
  CHECK(map.value()["module"] == "nocloom_groups_pair");
}
