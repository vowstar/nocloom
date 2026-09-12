#pragma once
// The strict YAML reader shared by the schema parser (yaml.cpp) and the
// shorthand expansion (shorthand.cpp). Internal to nocloom_config.
#include "nocloom/definition.hpp"
#include <array>
#include <charconv>
#include <initializer_list>
#include <span>
#include <string>
#include <string_view>
#include <yaml-cpp/yaml.h>

namespace nocloom::config::detail {
struct ParseFailure {
  Diagnostic diagnostic;
};
class Reader {
public:
  std::string_view text;
  std::string label;
  SourceMap sources;
  Reader(std::string_view input, std::string_view source) : text(input), label(source) {}
  [[noreturn]] void fail(std::string code, std::string path, std::string message,
                         const YAML::Mark &mark = YAML::Mark::null_mark()) const {
    Diagnostic d{std::move(code), std::move(path), std::move(message), {}, {}};
    if (!mark.is_null())
      d.location = SourceLocation{label, static_cast<unsigned>(mark.line + 1),
                                  static_cast<unsigned>(mark.column + 1)};
    throw ParseFailure{std::move(d)};
  }
  // Records where a definition field, also an expanded one, comes from.
  void note(const std::string &path, const YAML::Mark &mark) {
    sources[path] = {label, static_cast<unsigned>(mark.line + 1),
                     static_cast<unsigned>(mark.column + 1)};
  }
  YAML::Node field(const YAML::Node &node, const std::string &name, const std::string &path) {
    auto result = node[name];
    if (!result)
      fail("NCL_SCHEMA_MISSING_FIELD", path, "Required field is missing", node.Mark());
    sources[path] = {label, static_cast<unsigned>(result.Mark().line + 1),
                     static_cast<unsigned>(result.Mark().column + 1)};
    return result;
  }
  void object(const YAML::Node &node, const std::string &path,
              std::initializer_list<std::string_view> keys) const {
    object(node, path, std::span<const std::string_view>(keys.begin(), keys.size()));
  }
  void object(const YAML::Node &node, const std::string &path,
              std::span<const std::string_view> keys) const {
    if (!node.IsMap())
      fail("NCL_SCHEMA_TYPE", path, "Expected an object", node.Mark());
    for (const auto &entry : node) {
      auto key = entry.first.Scalar();
      bool known = false;
      for (auto allowed : keys)
        known = known || key == allowed;
      if (!known)
        fail("NCL_SCHEMA_UNKNOWN_FIELD", path.empty() ? key : path + "." + key, "Unknown field",
             entry.first.Mark());
    }
  }
  std::string string(const YAML::Node &node, const std::string &path) const {
    if (!node.IsScalar())
      fail("NCL_SCHEMA_TYPE", path, "Expected a string", node.Mark());
    const auto value = node.Scalar();
    if (node.Tag() != "!" && (value == "true" || value == "false" || value == "True" ||
                              value == "False" || value == "TRUE" || value == "FALSE" ||
                              (!value.empty() && ((value.front() >= '0' && value.front() <= '9') ||
                                                  value.front() == '+' || value.front() == '-'))))
      fail("NCL_SCHEMA_TYPE", path, "Expected a string, without implicit conversion", node.Mark());
    return value;
  }
  unsigned integer(const YAML::Node &node, const std::string &path) const {
    if (!node.IsScalar() || node.Tag() == "!")
      fail("NCL_SCHEMA_TYPE", path, "Expected an unquoted decimal integer", node.Mark());
    const auto &value = node.Scalar();
    if (value.empty() || (value.size() > 1 && value[0] == '0') ||
        value.find_first_not_of("0123456789") != std::string::npos)
      fail("NCL_SCHEMA_TYPE", path, "Expected an unquoted decimal integer", node.Mark());
    unsigned result = 0;
    auto converted = std::from_chars(value.data(), value.data() + value.size(), result);
    if (converted.ec != std::errc{})
      fail("NCL_SCHEMA_TYPE", path, "Integer is out of range", node.Mark());
    return result;
  }
  bool boolean(const YAML::Node &node, const std::string &path) const {
    if (!node.IsScalar() || node.Tag() == "!" ||
        (node.Scalar() != "true" && node.Scalar() != "false"))
      fail("NCL_SCHEMA_TYPE", path, "Expected unquoted true or false", node.Mark());
    return node.Scalar() == "true";
  }
  bool single_line_quote(const YAML::Mark &mark) const {
    if (mark.pos < 0 || static_cast<std::size_t>(mark.pos) >= text.size())
      return false;
    const auto pos = static_cast<std::size_t>(mark.pos);
    const char quote = text[pos];
    if (quote != '\'' && quote != '"')
      return false;
    for (std::size_t i = pos + 1; i < text.size(); ++i) {
      if (text[i] == '\n' || text[i] == '\r')
        return false;
      if (quote == '"' && text[i] == '\\') {
        if (++i >= text.size() || text[i] == '\n' || text[i] == '\r')
          return false;
        continue;
      }
      if (text[i] == quote) {
        if (quote == '\'' && i + 1 < text.size() && text[i + 1] == quote) {
          ++i;
          continue;
        }
        return true;
      }
    }
    return false;
  }
  std::uint64_t address(const YAML::Node &node, const std::string &path) const {
    if (!node.IsScalar() || !single_line_quote(node.Mark()))
      fail("NCL_SCHEMA_TYPE", path, "Address must be a single-line quoted string", node.Mark());
    auto value = std::string_view(node.Scalar());
    int radix = 10;
    if (value.starts_with("0x")) {
      radix = 16;
      value.remove_prefix(2);
    }
    if (value.empty() || (radix == 10 && value.size() > 1 && value[0] == '0') ||
        value.find_first_not_of(radix == 10 ? "0123456789" : "0123456789abcdefABCDEF") !=
            std::string_view::npos)
      fail("NCL_ADDRESS_RANGE", path, "Invalid address string", node.Mark());
    std::uint64_t result = 0;
    auto converted = std::from_chars(value.data(), value.data() + value.size(), result, radix);
    if (converted.ec != std::errc{})
      fail("NCL_ADDRESS_RANGE", path, "Address exceeds uint64 range", node.Mark());
    return result;
  }
  void sequence(const YAML::Node &node, const std::string &path) const {
    if (!node.IsSequence())
      fail("NCL_SCHEMA_TYPE", path, "Expected a sequence", node.Mark());
  }
};

// The fields an initiator or target entry may set; templates share them.
std::span<const std::string_view> initiator_fields();
std::span<const std::string_view> target_fields();
// An optional link pipe; endpoint pipes accept only their documented subset.
void pipeline(Reader &r, const YAML::Node &item, const std::string &field, const char *name,
              LinkPipeline &out);
// Replaces each entry's `use` by the fields of the templates it names.
void expand_templates(Reader &r, const YAML::Node &root);
// The connectivity matrix, with initiator and target groups, all and all_except expanded.
Connectivity connectivity(Reader &r, const YAML::Node &root);
// Node defaults of a graph: the pipes that output_links and input_links of a
// node give each link leaving or entering it that sets no such pipe itself.
// Slots: output pipeline, output sender_pipeline, input pipeline, input sender_pipeline.
struct NodePipes {
  std::array<std::optional<LinkPipeline>, 4> pipes;
  std::array<YAML::Mark, 4> marks;
};
NodePipes node_pipes(Reader &r, const YAML::Node &item, const std::string &field);
// Whether a link sets pipeline and sender_pipeline itself, and where it is written.
struct LinkPipes {
  bool pipeline = false, sender_pipeline = false;
  YAML::Mark mark;
};
void apply_node_pipes(const Reader &r, const std::string &path,
                      const std::vector<NodePipes> &defaults, const std::vector<LinkPipes> &links,
                      ExchangeGraph &graph);
// The response graph mirrored from the request graph.
ExchangeGraph mirror(Reader &r, const ExchangeGraph &request, const YAML::Node &spec,
                     const std::string &path);
} // namespace nocloom::config::detail
