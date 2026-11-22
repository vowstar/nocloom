#include "nocloom/config.hpp"
#include "reader.hpp"
#include <algorithm>
#include <set>
#include <sstream>
#include <yaml-cpp/eventhandler.h>
#include <yaml-cpp/parser.h>
#include <yaml-cpp/yaml.h>

namespace nocloom::config {
namespace {
using detail::ParseFailure;
using detail::Reader;
template <class Endpoint>
void wishbone_fields(Reader &r, const YAML::Node &node, const std::string &path, Endpoint &out) {
  if (node["address_unit"]) {
    const auto at = path + ".address_unit";
    const auto value = r.field(node, "address_unit", at);
    const auto unit = r.string(value, at);
    if (unit == "byte")
      out.address_unit = AddressUnit::Byte;
    else if (unit == "word")
      out.address_unit = AddressUnit::Word;
    else
      r.fail("NCL_WISHBONE_ADDRESS_UNIT", at, "Expected byte or word", value.Mark());
  }
  if (node["cycles"]) {
    const auto at = path + ".cycles";
    const auto value = r.field(node, "cycles", at);
    const auto mode = r.string(value, at);
    if (mode == "classic")
      out.cycles = WishboneCycles::Classic;
    else if (mode == "pipelined")
      out.cycles = WishboneCycles::Pipelined;
    else
      r.fail("NCL_WISHBONE_CYCLES", at, "Expected classic or pipelined", value.Mark());
  }
  if (node["burst_tags"])
    out.burst_tags =
        r.boolean(r.field(node, "burst_tags", path + ".burst_tags"), path + ".burst_tags");
  if (node["retry"]) {
    const auto at = path + ".retry";
    const auto value = r.field(node, "retry", at);
    if (r.string(value, at) != "error")
      r.fail("NCL_WISHBONE_OPTION", at, "Only retry: error is supported", value.Mark());
    out.retry = WishboneRetry::Error;
  }
}

Ahb5Properties ahb5_properties(Reader &r, const YAML::Node &node, const std::string &path) {
  r.object(node, path,
           {"hprot_width", "hnonsec", "hmaster_width", "exclusive", "hauser_width", "hwuser_width",
            "hruser_width", "hbuser_width"});
  Ahb5Properties out;
  if (node["hnonsec"])
    out.hnonsec = r.boolean(node["hnonsec"], path + ".hnonsec");
  if (node["exclusive"])
    out.exclusive = r.boolean(node["exclusive"], path + ".exclusive");
  for (const auto &[key, value] : {std::pair{"hprot_width", &out.hprot_width},
                                   {"hmaster_width", &out.hmaster_width},
                                   {"hauser_width", &out.hauser_width},
                                   {"hwuser_width", &out.hwuser_width},
                                   {"hruser_width", &out.hruser_width},
                                   {"hbuser_width", &out.hbuser_width}})
    if (node[key])
      *value = r.integer(node[key], path + "." + key);
  return out;
}
// One enumerated field of the axi5 object: its key, member and value names in enum order.
template <class Enum> struct Axi5Choice {
  const char *key = nullptr;
  Enum Axi5Properties::*member = nullptr;
  std::vector<const char *> names;
};
template <class Enum>
void axi5_choice(Reader &r, const YAML::Node &node, const std::string &path,
                 const Axi5Choice<Enum> &choice, Axi5Properties &out) {
  if (!node[choice.key])
    return;
  const auto at = path + "." + choice.key;
  const auto value = r.field(node, choice.key, at);
  const auto name = r.string(value, at);
  const auto found = std::ranges::find(choice.names, name);
  if (found == choice.names.end()) {
    std::string list;
    for (std::size_t k = 0; k < choice.names.size(); ++k)
      list += std::string(k == 0                         ? ""
                          : k + 1 == choice.names.size() ? " or "
                                                         : ", ") +
              choice.names[k];
    r.fail("NCL_AXI5_PROPERTIES", at, "Expected " + list, value.Mark());
  }
  out.*choice.member = static_cast<Enum>(found - choice.names.begin());
}
Axi5Properties axi5_properties(Reader &r, const YAML::Node &node, const std::string &path) {
  using P = Axi5Properties;
  static const std::vector<std::pair<const char *, bool P::*>> flags{
      {"nsaid", &P::nsaid},
      {"pbha", &P::pbha},
      {"trace", &P::trace},
      {"poison", &P::poison},
      {"unique_id", &P::unique_id},
      {"prot", &P::prot},
      {"priv_inst", &P::priv_inst},
      {"cache", &P::cache},
      {"consistent_decerr", &P::consistent_decerr},
      {"regular_only", &P::regular_only},
      {"wrap_line_only", &P::wrap_line_only},
      {"no_fixed_bursts", &P::no_fixed_bursts},
      {"device_normal_independence", &P::device_normal_independence},
      {"wakeup", &P::wakeup},
      {"qos_accept", &P::qos_accept},
      {"busy", &P::busy},
      {"read_chunking", &P::read_chunking},
      {"atomic", &P::atomic},
      {"rme", &P::rme},
      {"gdi", &P::gdi},
      {"mec", &P::mec},
      {"cmo_on_read", &P::cmo_on_read},
      {"cmo_on_write", &P::cmo_on_write},
      {"write_plus_cmo", &P::write_plus_cmo},
      {"persist_cmo", &P::persist_cmo},
      {"storage_cmo", &P::storage_cmo},
      {"prefetch", &P::prefetch},
      {"write_zero", &P::write_zero},
      {"write_deferrable", &P::write_deferrable},
      {"write_no_snoop_full", &P::write_no_snoop_full},
      {"invalidate_hint", &P::invalidate_hint},
      {"deallocation", &P::deallocation},
      {"shareable_cache", &P::shareable_cache},
      {"coherency_connection", &P::coherency_connection},
      {"act", &P::act}};
  static const std::vector<std::pair<const char *, unsigned P::*>> numbers{
      {"subsystem_id_width", &P::subsystem_id_width},
      {"write_loop_width", &P::write_loop_width},
      {"read_loop_width", &P::read_loop_width},
      {"pas_width", &P::pas_width},
      {"cache_line_bytes", &P::cache_line_bytes},
      {"max_transaction_bytes", &P::max_transaction_bytes}};
  static const std::vector<std::pair<const char *, std::optional<unsigned> P::*>> optional_numbers{
      {"subsystem_id", &P::subsystem_id},
      {"write_snoop_width", &P::write_snoop_width},
      {"read_snoop_width", &P::read_snoop_width}};
  static const std::vector<std::string_view> keys = [] {
    std::vector<std::string_view> all{"mpam",        "mte", "interface_check", "untranslated",
                                      "cache_stash", "dvm", "flow_control",    "shareable"};
    for (const auto &[key, member] : flags)
      all.emplace_back(key);
    for (const auto &[key, member] : numbers)
      all.emplace_back(key);
    for (const auto &[key, member] : optional_numbers)
      all.emplace_back(key);
    return all;
  }();
  r.object(node, path, std::span<const std::string_view>(keys));
  Axi5Properties out;
  for (const auto &[key, member] : flags)
    if (node[key])
      out.*member = r.boolean(node[key], path + "." + key);
  for (const auto &[key, member] : numbers)
    if (node[key])
      out.*member = r.integer(node[key], path + "." + key);
  for (const auto &[key, member] : optional_numbers)
    if (node[key])
      out.*member = r.integer(node[key], path + "." + key);
  if (node["shareable"])
    out.shareable = r.boolean(node["shareable"], path + ".shareable");
  axi5_choice<Axi5Mpam>(r, node, path, {"mpam", &P::mpam, {"none", "partid_9", "partid_12"}}, out);
  axi5_choice<Axi5Tagging>(r, node, path,
                           {"mte", &P::mte, {"none", "basic", "simplified", "standard"}}, out);
  axi5_choice<Axi5InterfaceCheck>(
      r, node, path, {"interface_check", &P::interface_check, {"none", "data", "all"}}, out);
  axi5_choice<Axi5Untranslated>(
      r, node, path, {"untranslated", &P::untranslated, {"none", "v1", "v2", "v3", "v4"}}, out);
  axi5_choice<Axi5CacheStash>(r, node, path,
                              {"cache_stash", &P::cache_stash, {"none", "basic", "full"}}, out);
  axi5_choice<Axi5Dvm>(r, node, path, {"dvm", &P::dvm, {"none", "receiver"}}, out);
  axi5_choice<Axi5FlowControl>(r, node, path,
                               {"flow_control", &P::flow_control, {"ready", "credited"}}, out);
  return out;
}
std::optional<EndpointClockGates> endpoint_gating(Reader &r, const YAML::Node &node,
                                                  const std::string &path) {
  if (!node["clock_gating"])
    return std::nullopt;
  const auto at = path + ".clock_gating";
  const auto field = r.field(node, "clock_gating", at);
  r.object(field, at, {"protocol", "transport", "single_gate"});
  EndpointClockGates gates;
  for (auto [key, value] :
       {std::pair{"protocol", &gates.protocol}, std::pair{"transport", &gates.transport},
        std::pair{"single_gate", &gates.single_gate}})
    if (field[key])
      *value = r.boolean(r.field(field, key, at + "." + key), at + "." + key);
  return gates;
}
// A preset name (amba, zero) or {preset, read_cache, write_cache, prot, hprot}
// whose bit lists are 0, 1 or a flag name, optionally prefixed by ~. Only the
// signals of the target protocol may be listed.
TargetAttributes target_attributes(Reader &r, const YAML::Node &node, const std::string &path,
                                   Protocol protocol) {
  auto preset = [&](const YAML::Node &value, const std::string &at) {
    const auto name = r.string(value, at);
    if (name == "amba")
      return TargetAttributes::amba();
    if (name != "zero")
      r.fail("NCL_TARGET_ATTRIBUTES", at, "Expected the amba or zero preset", value.Mark());
    return TargetAttributes::zero();
  };
  if (node.IsScalar())
    return preset(node, path);
  if (protocol == Protocol::Ahb5)
    r.fail("NCL_AHB5_PROPERTIES", path,
           "AHB5 does not support per-bit attribute overrides. Use attributes: amba", node.Mark());
  r.object(node, path, {"preset", "read_cache", "write_cache", "prot", "hprot"});
  auto result = node["preset"] ? preset(r.field(node, "preset", path + ".preset"), path + ".preset")
                               : TargetAttributes::zero();
  const bool axi = protocol == Protocol::Axi4 || protocol == Protocol::Axi3 ||
                   protocol == Protocol::Axi5 || protocol == Protocol::Ace5Lite;
  const bool prot = axi || protocol == Protocol::Axi4Lite ||
                    (protocol == Protocol::Apb4 || protocol == Protocol::Apb5);
  auto bits = [&](const char *key, bool allowed, auto &array) {
    if (!node[key])
      return;
    const auto at = path + "." + key;
    const auto list = r.field(node, key, at);
    if (!allowed)
      r.fail("NCL_TARGET_ATTRIBUTES", at, "This target protocol has no such attribute signal",
             list.Mark());
    r.sequence(list, at);
    if (list.size() != array.size())
      r.fail("NCL_TARGET_ATTRIBUTES", at,
             "Expected " + std::to_string(array.size()) + " bits, least significant first",
             list.Mark());
    for (std::size_t i = 0; i < list.size() && i < array.size(); ++i) {
      const auto item = list[i];
      const auto item_path = at + "[" + std::to_string(i) + "]";
      if (!item.IsScalar())
        r.fail("NCL_SCHEMA_TYPE", item_path, "Expected 0, 1 or a flag name", item.Mark());
      auto text = item.Scalar();
      AttributeBit bit;
      if (text.starts_with('~')) {
        bit.invert = true;
        text.erase(0, 1);
      }
      static const std::map<std::string, AttributeSource> names{
          {"0", AttributeSource::Zero},
          {"1", AttributeSource::One},
          {"bufferable", AttributeSource::Bufferable},
          {"modifiable", AttributeSource::Modifiable},
          {"allocate", AttributeSource::Allocate},
          {"lookup", AttributeSource::Lookup},
          {"privileged", AttributeSource::Privileged},
          {"nonsecure", AttributeSource::Nonsecure},
          {"instruction", AttributeSource::Instruction}};
      const auto it = names.find(text);
      if (it == names.end())
        r.fail("NCL_TARGET_ATTRIBUTES", item_path,
               "Expected 0, 1, bufferable, modifiable, allocate, lookup, privileged, "
               "nonsecure or instruction, optionally prefixed by ~",
               item.Mark());
      bit.source = it->second;
      array[i] = bit;
    }
  };
  bits("read_cache", axi, result.read_cache);
  bits("write_cache", axi, result.write_cache);
  bits("prot", prot, result.prot);
  bits("hprot", protocol == Protocol::AhbLite, result.hprot);
  return result;
}

// A user flag driver: 0, 1, user[i] or an attribute flag name, optionally prefixed by ~.
AttributeBit flag_driver(const Reader &r, const YAML::Node &item, const std::string &at) {
  if (!item.IsScalar())
    r.fail("NCL_SCHEMA_TYPE", at, "Expected 0, 1, user[i] or a flag name", item.Mark());
  auto text = item.Scalar();
  AttributeBit bit;
  if (text.starts_with('~')) {
    bit.invert = true;
    text.erase(0, 1);
  }
  static const std::map<std::string, AttributeSource> names{
      {"0", AttributeSource::Zero},
      {"1", AttributeSource::One},
      {"bufferable", AttributeSource::Bufferable},
      {"modifiable", AttributeSource::Modifiable},
      {"allocate", AttributeSource::Allocate},
      {"lookup", AttributeSource::Lookup},
      {"privileged", AttributeSource::Privileged},
      {"nonsecure", AttributeSource::Nonsecure},
      {"instruction", AttributeSource::Instruction}};
  if (const auto it = names.find(text); it != names.end()) {
    bit.source = it->second;
    return bit;
  }
  const auto digits = text.size() > 6 && text.starts_with("user[") && text.ends_with(']')
                          ? text.substr(5, text.size() - 6)
                          : std::string{};
  if (digits.empty() || digits.size() > 3 ||
      !std::ranges::all_of(digits, [](char c) { return c >= '0' && c <= '9'; }))
    r.fail("NCL_USER_FLAGS", at,
           "Expected 0, 1, user[i], bufferable, modifiable, allocate, lookup, privileged, "
           "nonsecure or instruction, optionally prefixed by ~",
           item.Mark());
  bit.source = AttributeSource::User;
  bit.index = static_cast<unsigned>(std::stoul(digits));
  return bit;
}
// A target AxUSER bit: 0, 1 or a network user flag name, optionally prefixed by ~.
UserBit user_bit(const Reader &r, const YAML::Node &item, const std::string &at) {
  if (!item.IsScalar())
    r.fail("NCL_SCHEMA_TYPE", at, "Expected 0, 1 or a user flag name", item.Mark());
  auto text = item.Scalar();
  UserBit bit;
  if (text.starts_with('~')) {
    bit.invert = true;
    text.erase(0, 1);
  }
  if (text == "0" || text == "1")
    bit.value = text == "1";
  else
    bit.flag = text;
  return bit;
}

// Arbitration policy names; validation decides where each is legal.
ArbitrationPolicy arbitration_policy(const Reader &r, const YAML::Node &node,
                                     const std::string &path) {
  static const std::map<std::string, ArbitrationPolicy> names{
      {"round_robin", ArbitrationPolicy::RoundRobin},
      {"fifo", ArbitrationPolicy::Fifo},
      {"rotating_priority", ArbitrationPolicy::RotatingPriority},
      {"fixed_priority", ArbitrationPolicy::FixedPriority},
      {"level_round_robin", ArbitrationPolicy::LevelRoundRobin}};
  const auto it = names.find(r.string(node, path));
  if (it == names.end())
    r.fail("NCL_UNSUPPORTED_ARBITRATION", path,
           "Expected round_robin, fifo, rotating_priority, fixed_priority or "
           "level_round_robin",
           node.Mark());
  return it->second;
}

class Guard final : public YAML::EventHandler {
  struct Frame {
    bool map = false;
    bool key = true;
    std::set<std::string> keys;
  };
  Reader &reader_;
  std::vector<Frame> stack_;
  std::size_t nodes_ = 0;
  unsigned documents_ = 0;
  void check(const YAML::Mark &mark, const std::string &tag, YAML::anchor_t anchor) {
    if (++nodes_ > max_nodes)
      reader_.fail("NCL_RESOURCE_LIMIT", "", "YAML node limit exceeded", mark);
    if (anchor != 0 || (tag != "?" && tag != "!" && !tag.empty()) ||
        (mark.pos >= 0 && static_cast<std::size_t>(mark.pos) < reader_.text.size() &&
         reader_.text[static_cast<std::size_t>(mark.pos)] == '!'))
      reader_.fail("NCL_SCHEMA_YAML_FEATURE", "", "Anchors and explicit tags are not supported",
                   mark);
  }
  void finish() {
    if (!stack_.empty() && stack_.back().map)
      stack_.back().key = !stack_.back().key;
  }
  void start(const YAML::Mark &mark, const std::string &tag, YAML::anchor_t anchor, bool map) {
    check(mark, tag, anchor);
    if (!stack_.empty() && stack_.back().map && stack_.back().key)
      reader_.fail("NCL_SCHEMA_TYPE", "", "Mapping keys must be scalar strings", mark);
    if (stack_.size() >= max_depth)
      reader_.fail("NCL_RESOURCE_LIMIT", "", "YAML nesting limit exceeded", mark);
    stack_.push_back({map, true, {}});
  }

public:
  explicit Guard(Reader &reader) : reader_(reader) {}
  void OnDocumentStart(const YAML::Mark &mark) override {
    if (++documents_ != 1)
      reader_.fail("NCL_SCHEMA_YAML_FEATURE", "", "Only one YAML document is accepted", mark);
  }
  void OnDocumentEnd() override {}
  void OnAnchor(const YAML::Mark &mark, const std::string &) override {
    reader_.fail("NCL_SCHEMA_YAML_FEATURE", "", "Anchors are not supported", mark);
  }
  void OnAlias(const YAML::Mark &mark, YAML::anchor_t) override {
    reader_.fail("NCL_SCHEMA_YAML_FEATURE", "", "Aliases are not supported", mark);
  }
  void OnNull(const YAML::Mark &mark, YAML::anchor_t anchor) override {
    check(mark, "", anchor);
    reader_.fail("NCL_SCHEMA_TYPE", "", "Null values are not supported", mark);
  }
  void OnScalar(const YAML::Mark &mark, const std::string &tag, YAML::anchor_t anchor,
                const std::string &value) override {
    check(mark, tag, anchor);
    if (!stack_.empty() && stack_.back().map && stack_.back().key) {
      if (value == "<<")
        reader_.fail("NCL_SCHEMA_YAML_FEATURE", "", "Merge keys are not supported", mark);
      if (!stack_.back().keys.insert(value).second)
        reader_.fail("NCL_SCHEMA_DUPLICATE_KEY", value, "Duplicate mapping key", mark);
    }
    finish();
  }
  void OnSequenceStart(const YAML::Mark &m, const std::string &t, YAML::anchor_t a,
                       YAML::EmitterStyle::value) override {
    start(m, t, a, false);
  }
  void OnSequenceEnd() override {
    stack_.pop_back();
    finish();
  }
  void OnMapStart(const YAML::Mark &m, const std::string &t, YAML::anchor_t a,
                  YAML::EmitterStyle::value) override {
    start(m, t, a, true);
  }
  void OnMapEnd() override {
    stack_.pop_back();
    finish();
  }
};
// The protocol of an endpoint by its name. An initiator also parses a name that only a target
// accepts: validation explains why an APB2 manager cannot be served.
Protocol endpoint_protocol(Reader &r, const YAML::Node &node, const std::string &path,
                           bool initiator) {
  const auto value = r.field(node, "protocol", path);
  const auto name = r.string(value, path);
  const auto &names = nocloom::detail::protocol_names;
  const auto *found = std::find_if(std::begin(names), std::end(names),
                                   [&](const auto &p) { return name == p.name; });
  // Other AXI5 interface classes of IHI 0022 Issue L, B2.1, and the coherent classes of earlier
  // issues: the class name and the protocol to use instead.
  static const std::map<std::string, std::pair<std::string, std::string>> classes{
      {"axi5_lite", {"AXI5-Lite", "axi5"}},
      {"ace5_lite_dvm", {"ACE5-LiteDVM", "ace5_lite"}},
      {"ace5_lite_acp", {"ACE5-LiteACP", "ace5_lite"}},
      {"ace5", {"ACE5", "ace5_lite"}},
      {"ace", {"ACE", "ace5_lite"}},
      {"ace_lite", {"ACE-Lite", "ace5_lite"}}};
  if (const auto it = classes.find(name); found == std::end(names) && it != classes.end())
    r.fail("NCL_UNSUPPORTED_PROTOCOL", path,
           "The " + it->second.first + " interface class is not supported. Use " +
               it->second.second,
           value.Mark());
  if (found == std::end(names))
    r.fail("NCL_UNSUPPORTED_PROTOCOL", path,
           (initiator ? "Initiators support " : "Targets support ") +
               nocloom::detail::supported_protocols(initiator),
           value.Mark());
  return found->protocol;
}
} // namespace

// error_logging: the window of the registers, a target of its own, and the logs.
void error_logging(Reader &r, const YAML::Node &node, Definition &d) {
  const std::string at = "error_logging";
  r.object(node, at, {"registers", "audit_flags", "logs", "revision", "capture_class"});
  if (node["capture_class"])
    d.error_logging.capture_class = r.boolean(node["capture_class"], at + ".capture_class");
  if (node["revision"])
    d.error_logging.revision =
        r.integer(r.field(node, "revision", at + ".revision"), at + ".revision");
  const auto names = [&](const YAML::Node &list, const std::string &path) {
    std::vector<std::string> out;
    r.sequence(list, path);
    for (std::size_t k = 0; k < list.size(); ++k)
      out.push_back(r.string(list[k], path + "[" + std::to_string(k) + "]"));
    return out;
  };
  const auto registers = r.field(node, "registers", at + ".registers");
  const auto window = at + ".registers";
  r.object(registers, window, {"name", "base", "size", "local_address", "clock_domain"});
  auto &target = d.targets.emplace_back(
      Target{r.string(r.field(registers, "name", window + ".name"), window + ".name"),
             r.string(r.field(registers, "clock_domain", window + ".clock_domain"),
                      window + ".clock_domain"),
             {}});
  auto &mapping = target.mappings.emplace_back();
  mapping.base = r.address(r.field(registers, "base", window + ".base"), window + ".base");
  mapping.size = r.address(r.field(registers, "size", window + ".size"), window + ".size");
  if (registers["local_address"])
    mapping.local_address = r.address(
        r.field(registers, "local_address", window + ".local_address"), window + ".local_address");
  target.protocol = Protocol::Apb4;
  target.data_width = 32;
  target.attributes = TargetAttributes::amba();
  target.log_registers = true;
  if (node["audit_flags"])
    d.error_logging.audit_flags = names(node["audit_flags"], at + ".audit_flags");
  const auto logs = r.field(node, "logs", at + ".logs");
  r.sequence(logs, at + ".logs");
  for (std::size_t i = 0; i < logs.size(); ++i) {
    const auto path = at + ".logs[" + std::to_string(i) + "]";
    const auto item = logs[i];
    r.object(item, path,
             {"name", "watch", "clock_domain", "whole_record", "loggers", "serial_word_bits"});
    auto &log = d.error_logging.logs.emplace_back();
    log.name = r.string(r.field(item, "name", path + ".name"), path + ".name");
    log.watch = names(r.field(item, "watch", path + ".watch"), path + ".watch");
    if (item["clock_domain"])
      log.clock_domain = r.string(item["clock_domain"], path + ".clock_domain");
    if (item["whole_record"])
      log.whole_record = r.boolean(item["whole_record"], path + ".whole_record");
    if (item["serial_word_bits"])
      log.serial_word_bits = r.integer(item["serial_word_bits"], path + ".serial_word_bits");
    if (!item["loggers"])
      continue;
    const auto loggers = item["loggers"];
    r.sequence(loggers, path + ".loggers");
    for (std::size_t k = 0; k < loggers.size(); ++k) {
      const auto here = path + ".loggers[" + std::to_string(k) + "]";
      const auto one = loggers[k];
      r.object(one, here, {"skip", "match", "pass_on", "offset"});
      auto &logger = log.loggers.emplace_back();
      if (one["skip"])
        logger.skip = names(one["skip"], here + ".skip");
      if (one["match"]) {
        if (!one["match"].IsMap())
          r.fail("NCL_SCHEMA_TYPE", here + ".match", "Expected a map of flag values",
                 one["match"].Mark());
        for (const auto &entry : one["match"])
          logger.match[r.string(entry.first, here + ".match")] =
              r.boolean(entry.second, here + ".match");
      }
      if (one["pass_on"]) {
        const auto how = r.string(one["pass_on"], here + ".pass_on");
        if (how == "always")
          logger.pass_on = ErrorLogger::PassOn::Always;
        else if (how != "unless_logged")
          r.fail("NCL_SCHEMA_TYPE", here + ".pass_on", "Expected always or unless_logged",
                 one["pass_on"].Mark());
      }
      if (one["offset"])
        logger.offset = r.address(one["offset"], here + ".offset");
    }
  }
}

Result<ParsedDefinition> parse(std::string_view text, std::string_view source_label) {
  Reader r(text, source_label);
  try {
    if (text.size() > max_input_bytes)
      r.fail("NCL_RESOURCE_LIMIT", "",
             "YAML input exceeds " + std::to_string(max_input_mib) + " MiB");
    std::istringstream stream{std::string(text)};
    YAML::Parser parser(stream);
    Guard guard(r);
    if (!parser.HandleNextDocument(guard))
      r.fail("NCL_SCHEMA_TYPE", "", "Expected a YAML document");
    while (parser.HandleNextDocument(guard)) {
    }
    auto root = YAML::Load(std::string(text));
    if (!root.IsMap())
      r.fail("NCL_SCHEMA_TYPE", "", "Expected an object", root.Mark());
    ParsedDefinition parsed;
    auto &d = parsed.definition;
    d.schema_version =
        r.integer(r.field(root, "schema_version", "schema_version"), "schema_version");
    if (d.schema_version >= 1 && d.schema_version <= 9)
      r.fail("NCL_SCHEMA_REMOVED", "schema_version", "Only schema version 10 is supported",
             root["schema_version"].Mark());
    if (d.schema_version != 10)
      r.fail("NCL_SCHEMA_VERSION", "schema_version", "Only schema version 10 is supported",
             root["schema_version"].Mark());
    // Every endpoint states its own protocol and data width. Network options
    // live under transport, the graphs under topology.
    r.object(root, "", {"schema_version",  "name",           "address_width",
                        "reset_ports",     "test_ports",     "enable_ports",
                        "ungate_ports",    "address_inputs", "address_selectors",
                        "clock_gating",    "reset_coverage", "cells",
                        "clock_roots",     "clock_domains",  "synchronizer_stages",
                        "clock_crossings", "initiators",     "targets",
                        "arbitration",     "transport",      "topology",
                        "connectivity",    "output",         "templates",
                        "groups",          "error_logging"});
    detail::expand_templates(r, root);
    if (root["arbitration"]) {
      auto arbitration = r.field(root, "arbitration", "arbitration");
      r.object(arbitration, "arbitration", {"policy"});
      d.arbitration =
          arbitration["policy"]
              ? arbitration_policy(r, r.field(arbitration, "policy", "arbitration.policy"),
                                   "arbitration.policy")
              : ArbitrationPolicy::RoundRobin;
    }
    d.name = r.string(r.field(root, "name", "name"), "name");
    if (root["transport"]) {
      auto transport = r.field(root, "transport", "transport");
      r.object(transport, "transport",
               {"data_width", "max_outstanding", "id_table_entries", "transport_word_bytes",
                "packet_limits", "user_flags", "response_user_bits", "data_user_bits_per_byte",
                "compact_failure_responses"});
      TransportOptions options;
      if (transport["compact_failure_responses"])
        options.compact_failure_responses = r.boolean(transport["compact_failure_responses"],
                                                      "transport.compact_failure_responses");
      if (transport["data_width"])
        options.data_width = r.integer(r.field(transport, "data_width", "transport.data_width"),
                                       "transport.data_width");
      const auto number = [&](const char *key, unsigned &out) {
        const auto path = std::string("transport.") + key;
        if (transport[key])
          out = r.integer(r.field(transport, key, path), path);
      };
      number("max_outstanding", options.max_outstanding);
      number("response_user_bits", options.response_user_bits);
      number("data_user_bits_per_byte", options.data_user_bits_per_byte);
      if (transport["id_table_entries"])
        options.id_table_entries =
            r.integer(r.field(transport, "id_table_entries", "transport.id_table_entries"),
                      "transport.id_table_entries");
      if (transport["transport_word_bytes"])
        options.transport_word_bytes =
            r.integer(r.field(transport, "transport_word_bytes", "transport.transport_word_bytes"),
                      "transport.transport_word_bytes");
      if (transport["packet_limits"]) {
        const auto limits = r.field(transport, "packet_limits", "transport.packet_limits");
        r.sequence(limits, "transport.packet_limits");
        for (std::size_t i = 0; i < limits.size(); ++i) {
          const auto field = "transport.packet_limits[" + std::to_string(i) + "]";
          const auto item = limits[i];
          r.object(item, field, {"source", "target", "max_bytes", "boundary_bytes"});
          PacketLimit limit{
              r.string(r.field(item, "source", field + ".source"), field + ".source"),
              r.string(r.field(item, "target", field + ".target"), field + ".target")};
          if (item["max_bytes"])
            limit.max_bytes =
                r.integer(r.field(item, "max_bytes", field + ".max_bytes"), field + ".max_bytes");
          if (item["boundary_bytes"])
            limit.boundary_bytes =
                r.integer(r.field(item, "boundary_bytes", field + ".boundary_bytes"),
                          field + ".boundary_bytes");
          options.packet_limits.push_back(std::move(limit));
        }
      }
      if (transport["user_flags"]) {
        const auto flags = r.field(transport, "user_flags", "transport.user_flags");
        r.sequence(flags, "transport.user_flags");
        for (std::size_t i = 0; i < flags.size(); ++i)
          options.user_flags.push_back(
              r.string(flags[i], "transport.user_flags[" + std::to_string(i) + "]"));
      }
      d.transport = options;
    }
    // An optional pipe of a link, an attachment or an endpoint.
    auto pipeline = [&](const YAML::Node &item, const std::string &field, const char *name,
                        LinkPipeline &out) { detail::pipeline(r, item, field, name, out); };
    if (root["topology"]) {
      const auto topology = r.field(root, "topology", "topology");
      r.object(topology, "topology", {"request", "response", "routes"});
      auto graph = [&](const char *direction) {
        const auto path = std::string("topology.") + direction;
        const auto node = r.field(topology, direction, path);
        r.object(node, path, {"nodes", "links", "entries", "exits"});
        ExchangeGraph result;
        std::vector<detail::NodePipes> defaults;
        std::vector<detail::LinkPipes> link_set;
        auto nodes = r.field(node, "nodes", path + ".nodes");
        r.sequence(nodes, path + ".nodes");
        for (std::size_t i = 0; i < nodes.size(); ++i) {
          const auto field = path + ".nodes[" + std::to_string(i) + "]";
          const auto item = nodes[i];
          r.object(item, field,
                   {"name", "transport_word_bytes", "clock_domain", "arbitration", "output_links",
                    "input_links"});
          ExchangeNode n{r.string(r.field(item, "name", field + ".name"), field + ".name")};
          if (item["transport_word_bytes"])
            n.transport_word_bytes =
                r.integer(item["transport_word_bytes"], field + ".transport_word_bytes");
          if (item["clock_domain"])
            n.clock_domain = r.string(item["clock_domain"], field + ".clock_domain");
          if (item["arbitration"]) {
            const auto list = r.field(item, "arbitration", field + ".arbitration");
            r.sequence(list, field + ".arbitration");
            for (std::size_t j = 0; j < list.size(); ++j) {
              const auto at = field + ".arbitration[" + std::to_string(j) + "]";
              r.object(list[j], at, {"output", "policy", "priority"});
              OutputArbitration o{
                  r.string(r.field(list[j], "output", at + ".output"), at + ".output"),
                  arbitration_policy(r, r.field(list[j], "policy", at + ".policy"), at + ".policy"),
                  {}};
              if (list[j]["priority"]) {
                const auto order = r.field(list[j], "priority", at + ".priority");
                r.sequence(order, at + ".priority");
                for (std::size_t k = 0; k < order.size(); ++k)
                  o.priority.push_back(
                      r.string(order[k], at + ".priority[" + std::to_string(k) + "]"));
              }
              n.arbitration.push_back(std::move(o));
            }
          }
          result.nodes.push_back(std::move(n));
          defaults.push_back(detail::node_pipes(r, item, field));
        }
        auto links = r.field(node, "links", path + ".links");
        r.sequence(links, path + ".links");
        for (std::size_t i = 0; i < links.size(); ++i) {
          const auto field = path + ".links[" + std::to_string(i) + "]";
          const auto item = links[i];
          r.object(item, field,
                   {"from", "to", "transport_word_bytes", "pipeline", "sender_pipeline", "stages"});
          ExchangeLink link{r.string(r.field(item, "from", field + ".from"), field + ".from"),
                            r.string(r.field(item, "to", field + ".to"), field + ".to")};
          if (item["transport_word_bytes"])
            link.transport_word_bytes =
                r.integer(item["transport_word_bytes"], field + ".transport_word_bytes");
          pipeline(item, field, "pipeline", link.pipeline);
          pipeline(item, field, "sender_pipeline", link.sender_pipeline);
          detail::stage_lists(r, item, field, link.stages);
          link_set.push_back(
              {item["pipeline"].IsDefined(), item["sender_pipeline"].IsDefined(), item.Mark()});
          result.links.push_back(std::move(link));
        }
        for (const auto *key : {"entries", "exits"}) {
          const auto field = path + "." + key;
          auto items = r.field(node, key, field);
          r.sequence(items, field);
          auto &dest = std::string_view(key) == "entries" ? result.entries : result.exits;
          for (std::size_t i = 0; i < items.size(); ++i) {
            const auto part = field + "[" + std::to_string(i) + "]";
            r.object(items[i], part, {"endpoint", "node", "pipeline"});
            dest.push_back(
                {r.string(r.field(items[i], "endpoint", part + ".endpoint"), part + ".endpoint"),
                 r.string(r.field(items[i], "node", part + ".node"), part + ".node")});
            pipeline(items[i], part, "pipeline", dest.back().pipeline);
          }
        }
        detail::apply_node_pipes(r, path, defaults, link_set, result);
        return result;
      };
      auto request = graph("request");
      // response: mirror, or {mirror: {...}}, derives the response graph from the request graph.
      const auto response = topology["response"];
      const bool mirrored = (response.IsScalar() && response.Scalar() == "mirror") ||
                            (response.IsMap() && response["mirror"].IsDefined());
      d.topology = ExchangeTopology{
          request,
          mirrored ? detail::mirror(r, request, r.field(topology, "response", "topology.response"),
                                    "topology.response")
                   : graph("response")};
      if (topology["routes"]) {
        const auto routes = r.field(topology, "routes", "topology.routes");
        r.sequence(routes, "topology.routes");
        for (std::size_t i = 0; i < routes.size(); ++i) {
          const auto field = "topology.routes[" + std::to_string(i) + "]";
          const auto item = routes[i];
          r.object(item, field,
                   {"initiator", "source_path", "target", "target_path", "request", "response"});
          ExchangeRoute route{
              r.string(r.field(item, "initiator", field + ".initiator"), field + ".initiator"),
              r.string(r.field(item, "target", field + ".target"), field + ".target")};
          if (item["source_path"])
            route.source_path = r.integer(r.field(item, "source_path", field + ".source_path"),
                                          field + ".source_path");
          if (item["target_path"])
            route.target_path = r.integer(r.field(item, "target_path", field + ".target_path"),
                                          field + ".target_path");
          for (const auto *key : {"request", "response"}) {
            if (!item[key])
              continue;
            const auto at = field + "." + key;
            const auto path = r.field(item, key, at);
            r.sequence(path, at);
            if (path.size() == 0)
              r.fail("NCL_ROUTE_PATH", at, "A listed path names at least one node", path.Mark());
            auto &nodes = std::string_view(key) == "request" ? route.request : route.response;
            for (std::size_t j = 0; j < path.size(); ++j)
              nodes.push_back(r.string(path[j], at + "[" + std::to_string(j) + "]"));
          }
          d.topology->routes.push_back(std::move(route));
        }
      }
    }
    // Connectivity: all, or a map from each source (or initiator group) to its targets.
    if (root["connectivity"])
      d.connectivity = detail::connectivity(r, root);
    d.address_width = r.integer(r.field(root, "address_width", "address_width"), "address_width");
    auto resets = r.field(root, "reset_ports", "reset_ports");
    r.sequence(resets, "reset_ports");
    for (std::size_t i = 0; i < resets.size(); ++i) {
      auto node = resets[i];
      const auto path = "reset_ports[" + std::to_string(i) + "]";
      r.object(node, path, {"name"});
      d.reset_ports.push_back({r.string(r.field(node, "name", path + ".name"), path + ".name")});
    }
    if (root["test_ports"]) {
      auto tests = r.field(root, "test_ports", "test_ports");
      r.sequence(tests, "test_ports");
      for (std::size_t i = 0; i < tests.size(); ++i) {
        const auto path = "test_ports[" + std::to_string(i) + "]";
        r.object(tests[i], path, {"name"});
        d.test_ports.push_back(
            {r.string(r.field(tests[i], "name", path + ".name"), path + ".name")});
      }
    }
    for (const auto *key : {"enable_ports", "ungate_ports"}) {
      if (!root[key])
        continue;
      auto list = r.field(root, key, key);
      r.sequence(list, key);
      for (std::size_t i = 0; i < list.size(); ++i) {
        const auto path = key + std::string("[") + std::to_string(i) + "]";
        r.object(list[i], path, {"name"});
        const auto name = r.string(r.field(list[i], "name", path + ".name"), path + ".name");
        if (std::string_view(key) == "enable_ports")
          d.enable_ports.push_back({name});
        else
          d.ungate_ports.push_back({name});
      }
    }
    if (root["address_inputs"]) {
      auto list = r.field(root, "address_inputs", "address_inputs");
      r.sequence(list, "address_inputs");
      for (std::size_t i = 0; i < list.size(); ++i) {
        const auto path = "address_inputs[" + std::to_string(i) + "]";
        r.object(list[i], path, {"name", "width", "clock_domain"});
        auto &port = d.address_inputs.emplace_back(AddressInput{
            r.string(r.field(list[i], "name", path + ".name"), path + ".name"), 1, {}});
        if (list[i]["width"])
          port.width = r.integer(list[i]["width"], path + ".width");
        port.clock_domain = r.string(r.field(list[i], "clock_domain", path + ".clock_domain"),
                                     path + ".clock_domain");
      }
    }
    if (root["address_selectors"]) {
      auto list = r.field(root, "address_selectors", "address_selectors");
      r.sequence(list, "address_selectors");
      for (std::size_t i = 0; i < list.size(); ++i) {
        const auto path = "address_selectors[" + std::to_string(i) + "]";
        r.object(list[i], path, {"name", "equals"});
        auto &flag = d.address_selectors.emplace_back(AddressSelector{
            r.string(r.field(list[i], "name", path + ".name"), path + ".name"), {}});
        const auto equals = r.field(list[i], "equals", path + ".equals");
        if (!equals.IsMap())
          r.fail("NCL_SCHEMA_TYPE", path + ".equals", "Expected an object of address input values",
                 equals.Mark());
        for (const auto &entry : equals) {
          const auto at = path + ".equals." + entry.first.Scalar();
          flag.equals[entry.first.Scalar()] =
              r.integer(r.field(equals, entry.first.Scalar(), at), at);
        }
      }
    }
    if (root["clock_gating"])
      d.clock_gating = r.boolean(r.field(root, "clock_gating", "clock_gating"), "clock_gating");
    if (root["reset_coverage"]) {
      auto registers = r.field(root, "reset_coverage", "reset_coverage");
      const auto kind = r.string(registers, "reset_coverage");
      if (kind != "all" && kind != "minimal")
        r.fail("NCL_SCHEMA_TYPE", "reset_coverage", "Expected all or minimal", registers.Mark());
      d.reset_coverage = kind == "all" ? ResetCoverage::All : ResetCoverage::Minimal;
    }
    if (root["cells"]) {
      // Each cell: module and role pin names, and tie for its other input pins.
      const auto cells = r.field(root, "cells", "cells");
      r.object(cells, "cells", {"clock_gate", "synchronizer"});
      auto cell = [&r, &cells](const char *key, std::initializer_list<std::string_view> keys) {
        const auto path = std::string("cells.") + key;
        const auto node = r.field(cells, key, path);
        r.object(node, path, keys);
        auto pin = [&r, node, path](const char *role) {
          return r.string(r.field(node, role, path + "." + role), path + "." + role);
        };
        std::map<std::string, bool> tie;
        if (node["tie"]) {
          const auto ties = r.field(node, "tie", path + ".tie");
          if (!ties.IsMap())
            r.fail("NCL_SCHEMA_TYPE", path + ".tie", "Expected an object", ties.Mark());
          for (const auto &entry : ties) {
            const auto at = path + ".tie." + entry.first.Scalar();
            const auto value = r.integer(r.field(ties, entry.first.Scalar(), at), at);
            if (value > 1)
              r.fail("NCL_SCHEMA_TYPE", at, "Expected 0 or 1", entry.second.Mark());
            tie[entry.first.Scalar()] = value == 1;
          }
        }
        return std::tuple{node, pin, tie};
      };
      if (cells["clock_gate"]) {
        auto [node, pin, tie] =
            cell("clock_gate", {"module", "clock", "enable", "test", "output", "tie"});
        d.cells.clock_gate =
            ClockGateCell{pin("module"), pin("clock"),
                          pin("enable"), node["test"] ? std::optional{pin("test")} : std::nullopt,
                          pin("output"), tie};
      }
      if (cells["synchronizer"]) {
        auto [node, pin, tie] = cell("synchronizer", {"module", "stages", "clock", "data", "reset",
                                                      "reset_active", "output", "tie"});
        const auto active = pin("reset_active");
        if (active != "low" && active != "high")
          r.fail("NCL_SCHEMA_TYPE", "cells.synchronizer.reset_active", "Expected low or high",
                 node["reset_active"].Mark());
        d.cells.synchronizer =
            SynchronizerBinding{pin("module"),
                                r.integer(r.field(node, "stages", "cells.synchronizer.stages"),
                                          "cells.synchronizer.stages"),
                                pin("clock"),
                                pin("data"),
                                pin("reset"),
                                active == "low",
                                pin("output"),
                                tie};
      }
    }
    if (root["clock_roots"]) {
      auto roots = r.field(root, "clock_roots", "clock_roots");
      r.sequence(roots, "clock_roots");
      for (std::size_t i = 0; i < roots.size(); ++i) {
        const auto node = roots[i];
        const auto path = "clock_roots[" + std::to_string(i) + "]";
        r.object(node, path, {"name", "clock_port", "period_ps"});
        ClockRoot root_clock{
            r.string(r.field(node, "name", path + ".name"), path + ".name"),
            r.string(r.field(node, "clock_port", path + ".clock_port"), path + ".clock_port")};
        if (node["period_ps"])
          root_clock.period_ps =
              r.integer<std::uint64_t>(r.field(node, "period_ps", path + ".period_ps"),
                                       path + ".period_ps", "NCL_CDC_PERIOD_RANGE");
        d.clock_roots.push_back(std::move(root_clock));
      }
    }
    auto domains = r.field(root, "clock_domains", "clock_domains");
    r.sequence(domains, "clock_domains");
    for (std::size_t i = 0; i < domains.size(); ++i) {
      auto node = domains[i];
      const auto path = "clock_domains[" + std::to_string(i) + "]";
      r.object(node, path,
               {"name", "clock_port", "reset_port", "period_ps", "test_port", "gated", "ratio",
                "enable_port", "clock_root", "edge_parent", "ungate_port", "reset_synchronizer"});
      auto field = [&](const char *key) {
        return r.string(r.field(node, key, path + "." + key), path + "." + key);
      };
      d.clock_domains.push_back({field("name"), field("clock_port"), field("reset_port")});
      if (node["period_ps"])
        d.clock_domains.back().period_ps =
            r.integer<std::uint64_t>(r.field(node, "period_ps", path + ".period_ps"),
                                     path + ".period_ps", "NCL_CDC_PERIOD_RANGE");
      if (node["test_port"])
        d.clock_domains.back().test_port = field("test_port");
      if (node["gated"])
        d.clock_domains.back().gated = r.boolean(node["gated"], path + ".gated");
      if (node["reset_synchronizer"])
        d.clock_domains.back().reset_synchronizer =
            r.boolean(node["reset_synchronizer"], path + ".reset_synchronizer");
      if (node["ratio"])
        d.clock_domains.back().ratio =
            r.integer(r.field(node, "ratio", path + ".ratio"), path + ".ratio");
      for (auto [key, out] : {std::pair{"enable_port", &d.clock_domains.back().enable_port},
                              std::pair{"clock_root", &d.clock_domains.back().clock_root},
                              std::pair{"edge_parent", &d.clock_domains.back().edge_parent},
                              std::pair{"ungate_port", &d.clock_domains.back().ungate_port}})
        if (node[key])
          *out = field(key);
    }
    if (root["synchronizer_stages"])
      d.synchronizer_stages = r.integer(r.field(root, "synchronizer_stages", "synchronizer_stages"),
                                        "synchronizer_stages");
    if (root["clock_crossings"]) {
      auto crossings = r.field(root, "clock_crossings", "clock_crossings");
      r.sequence(crossings, "clock_crossings");
      for (std::size_t i = 0; i < crossings.size(); ++i) {
        const auto path = "clock_crossings[" + std::to_string(i) + "]";
        const auto item = crossings[i];
        r.object(item, path,
                 {"from_domain", "to_domain", "kind", "depth", "synchronizer_stages",
                  "reset_handshake", "slow_pipeline", "fast"});
        ClockCrossing crossing{
            r.string(r.field(item, "from_domain", path + ".from_domain"), path + ".from_domain"),
            r.string(r.field(item, "to_domain", path + ".to_domain"), path + ".to_domain")};
        const auto kind = item["kind"]
                              ? r.string(r.field(item, "kind", path + ".kind"), path + ".kind")
                              : "async";
        auto reject_fields = [&](std::initializer_list<const char *> fields) {
          for (const auto *key : fields)
            if (item[key])
              r.fail("NCL_CDC_KIND_FIELD", path + "." + key,
                     "Field does not apply to crossing kind " + kind, item[key].Mark());
        };
        if (kind == "related") {
          reject_fields({"synchronizer_stages", "reset_handshake", "slow_pipeline"});
          SyncClockCrossing options;
          if (item["depth"])
            options.depth = r.integer(r.field(item, "depth", path + ".depth"), path + ".depth");
          if (item["fast"])
            options.fast = r.boolean(r.field(item, "fast", path + ".fast"), path + ".fast");
          crossing.options = options;
        } else if (kind == "edge_aligned") {
          reject_fields({"depth", "synchronizer_stages", "reset_handshake", "fast"});
          EdgeAlignedCrossing options;
          if (item["slow_pipeline"])
            options.slow_pipeline = r.boolean(
                r.field(item, "slow_pipeline", path + ".slow_pipeline"), path + ".slow_pipeline");
          crossing.options = options;
        } else if (kind == "async") {
          reject_fields({"slow_pipeline", "fast"});
          AsyncClockCrossing options;
          if (item["depth"])
            options.depth = r.integer(r.field(item, "depth", path + ".depth"), path + ".depth");
          if (item["synchronizer_stages"])
            options.synchronizer_stages =
                r.integer(r.field(item, "synchronizer_stages", path + ".synchronizer_stages"),
                          path + ".synchronizer_stages");
          if (item["reset_handshake"])
            options.reset_handshake =
                r.boolean(r.field(item, "reset_handshake", path + ".reset_handshake"),
                          path + ".reset_handshake");
          crossing.options = options;
        } else {
          r.fail("NCL_CDC_KIND", path + ".kind", "Expected async, related or edge_aligned",
                 item["kind"].Mark());
        }
        d.clock_crossings.push_back(std::move(crossing));
      }
    }
    const auto access = [&](const auto &node, const std::string &path) {
      const auto value = r.field(node, "access", path + ".access");
      const auto name = r.string(value, path + ".access");
      if (name == "read_only")
        return Access::ReadOnly;
      if (name == "write_only")
        return Access::WriteOnly;
      if (name != "read_write")
        r.fail("NCL_ENDPOINT_ACCESS", path + ".access",
               "Expected read_write, read_only or write_only", value.Mark());
      return Access::ReadWrite;
    };
    auto initiators = r.field(root, "initiators", "initiators");
    r.sequence(initiators, "initiators");
    for (std::size_t i = 0; i < initiators.size(); ++i) {
      auto node = initiators[i];
      const auto path = "initiators[" + std::to_string(i) + "]";
      // AxREGION exists only downstream of the address decode (IHI 0022 Issue L, A4.7.1).
      if (node.IsMap() && node["region_port"])
        r.fail("NCL_TARGET_REGION", path + ".region_port",
               "An initiator has no AxREGION port. A target drives AxREGION from the address map",
               node["region_port"].Mark());
      r.object(node, path, detail::initiator_fields());
      d.initiators.push_back({r.string(r.field(node, "name", path + ".name"), path + ".name"),
                              r.string(r.field(node, "clock_domain", path + ".clock_domain"),
                                       path + ".clock_domain")});
      d.initiators.back().clock_gating = endpoint_gating(r, node, path);
      if (node["slverr_only"])
        d.initiators.back().slverr_only = r.boolean(node["slverr_only"], path + ".slverr_only");
      if (node["source_width"])
        d.initiators.back().source_width = r.integer(
            r.field(node, "source_width", path + ".source_width"), path + ".source_width");
      if (node["size_width"])
        d.initiators.back().size_width =
            r.integer(r.field(node, "size_width", path + ".size_width"), path + ".size_width");
      if (node["sink_width"])
        d.initiators.back().sink_width =
            r.integer(r.field(node, "sink_width", path + ".sink_width"), path + ".sink_width");
      if (node["max_transfer_bytes"])
        d.initiators.back().max_transfer_bytes =
            r.integer(r.field(node, "max_transfer_bytes", path + ".max_transfer_bytes"),
                      path + ".max_transfer_bytes");
      if (node["pipes"]) {
        const auto at = path + ".pipes";
        const auto item = r.field(node, "pipes", at);
        r.object(item, at, {"request", "packets", "issue", "response"});
        auto &pipes = d.initiators.back().pipes;
        pipeline(item, at, "request", pipes.request);
        pipeline(item, at, "packets", pipes.packets);
        pipeline(item, at, "response", pipes.response);
        if (item["issue"])
          pipes.issue = r.boolean(item["issue"], at + ".issue");
      }
      if (node["qos"]) {
        const auto qos = r.field(node, "qos", path + ".qos");
        r.object(qos, path + ".qos", {"levels", "axqos_map", "shaper", "scheduler"});
        auto &q = d.initiators.back().qos;
        if (qos["axqos_map"]) {
          const auto at = path + ".qos.axqos_map";
          const auto list = r.field(qos, "axqos_map", at);
          r.sequence(list, at);
          for (std::size_t k = 0; k < list.size(); ++k)
            q.axqos_map.push_back(r.integer(list[k], at + "[" + std::to_string(k) + "]"));
        }
        if (qos["levels"]) {
          const auto at = path + ".qos.levels";
          const auto item = r.field(qos, "levels", at);
          r.object(item, at, {"read", "write"});
          if (item["read"])
            q.levels.read = r.integer(r.field(item, "read", at + ".read"), at + ".read");
          if (item["write"])
            q.levels.write = r.integer(r.field(item, "write", at + ".write"), at + ".write");
        }
        if (qos["shaper"]) {
          const auto at = path + ".qos.shaper";
          const auto item = r.field(qos, "shaper", at);
          r.object(item, at, {"bytes_per_1024_cycles", "burst_bytes", "bytes_per_32768_cycles"});
          const auto get = [&](const char *key) {
            return r.integer(r.field(item, key, at + "." + key), at + "." + key);
          };
          q.shaper = QosShaper{std::nullopt, get("burst_bytes")};
          if (item["bytes_per_1024_cycles"] || !item["bytes_per_32768_cycles"])
            q.shaper->bytes_per_1024_cycles = get("bytes_per_1024_cycles");
          if (item["bytes_per_32768_cycles"])
            q.shaper->bytes_per_32768_cycles = get("bytes_per_32768_cycles");
        }
        if (qos["scheduler"]) {
          const auto at = path + ".qos.scheduler";
          const auto item = r.field(qos, "scheduler", at);
          r.object(item, at, {"under_level", "over_level", "threshold_percent"});
          const auto get = [&](const char *key) {
            return r.integer(r.field(item, key, at + "." + key), at + "." + key);
          };
          q.scheduler =
              QosScheduler{get("under_level"), get("over_level"), get("threshold_percent")};
        }
      }
      wishbone_fields(r, node, path, d.initiators.back());
      if (node["ahb5"])
        d.initiators.back().ahb5 = ahb5_properties(r, node["ahb5"], path + ".ahb5");
      if (node["axi5"])
        d.initiators.back().axi5 = axi5_properties(r, node["axi5"], path + ".axi5");
      if (node["address_width"])
        d.initiators.back().address_width = r.integer(
            r.field(node, "address_width", path + ".address_width"), path + ".address_width");
      d.initiators.back().data_width =
          r.integer(r.field(node, "data_width", path + ".data_width"), path + ".data_width");
      if (node["id_width"])
        d.initiators.back().id_width =
            r.integer(r.field(node, "id_width", path + ".id_width"), path + ".id_width");
      if (node["len_width"])
        d.initiators.back().len_width =
            r.integer(r.field(node, "len_width", path + ".len_width"), path + ".len_width");
      if (node["access"])
        d.initiators.back().access = access(node, path);
      if (node["default_target"])
        d.initiators.back().default_target = r.string(
            r.field(node, "default_target", path + ".default_target"), path + ".default_target");
      if (node["user_width"])
        d.initiators.back().user_width =
            r.integer(r.field(node, "user_width", path + ".user_width"), path + ".user_width");
      if (node["data_user_width"])
        d.initiators.back().data_user_width = r.integer(
            r.field(node, "data_user_width", path + ".data_user_width"), path + ".data_user_width");
      if (node["response_user_width"])
        d.initiators.back().response_user_width =
            r.integer(r.field(node, "response_user_width", path + ".response_user_width"),
                      path + ".response_user_width");
      if (node["user_flags"]) {
        const auto at = path + ".user_flags";
        const auto flags = r.field(node, "user_flags", at);
        if (!flags.IsMap())
          r.fail("NCL_SCHEMA_TYPE", at, "Expected a map of user flag drivers", flags.Mark());
        for (const auto &entry : flags) {
          const auto name = r.string(entry.first, at);
          d.initiators.back().user_flags[name] = flag_driver(r, entry.second, at + "." + name);
        }
      }
      if (node["audit_flags"]) {
        const auto at = path + ".audit_flags";
        const auto flags = r.field(node, "audit_flags", at);
        if (!flags.IsMap())
          r.fail("NCL_SCHEMA_TYPE", at, "Expected a map of audit flag drivers", flags.Mark());
        for (const auto &entry : flags) {
          const auto name = r.string(entry.first, at);
          d.initiators.back().audit_flags[name] = flag_driver(r, entry.second, at + "." + name);
        }
      }
      if (node["route_classes"]) {
        const auto at = path + ".route_classes";
        const auto classes = r.field(node, "route_classes", at);
        r.sequence(classes, at);
        for (std::size_t k = 0; k < classes.size(); ++k)
          d.initiators.back().route_classes.push_back(
              r.string(classes[k], at + "[" + std::to_string(k) + "]"));
      }
      if (node["default_class"])
        d.initiators.back().default_class = r.string(
            r.field(node, "default_class", path + ".default_class"), path + ".default_class");
      if (node["id_ranges"]) {
        const auto at = path + ".id_ranges";
        const auto ranges = r.field(node, "id_ranges", at);
        r.sequence(ranges, at);
        for (std::size_t k = 0; k < ranges.size(); ++k) {
          const auto item = ranges[k];
          const auto here = at + "[" + std::to_string(k) + "]";
          r.object(item, here, {"first", "last", "class"});
          d.initiators.back().id_ranges.push_back(
              {r.integer(r.field(item, "first", here + ".first"), here + ".first"),
               r.integer(r.field(item, "last", here + ".last"), here + ".last"),
               r.string(r.field(item, "class", here + ".class"), here + ".class")});
        }
      }
      if (node["max_outstanding"])
        d.initiators.back().max_outstanding = r.integer(
            r.field(node, "max_outstanding", path + ".max_outstanding"), path + ".max_outstanding");
      if (node["id_table_entries"])
        d.initiators.back().id_table_entries =
            r.integer(r.field(node, "id_table_entries", path + ".id_table_entries"),
                      path + ".id_table_entries");
      if (node["narrow_bursts"]) {
        const auto value = r.field(node, "narrow_bursts", path + ".narrow_bursts");
        const auto name = r.string(value, path + ".narrow_bursts");
        if (name == "merge")
          d.initiators.back().narrow_bursts = NarrowBursts::Merge;
        else if (name == "merge_modifiable")
          d.initiators.back().narrow_bursts = NarrowBursts::MergeModifiable;
        else
          r.fail("NCL_AXI4_NARROW_BURSTS", path + ".narrow_bursts",
                 "Expected merge or merge_modifiable", value.Mark());
      }
      d.initiators.back().protocol = endpoint_protocol(r, node, path + ".protocol", true);
    }
    auto targets = r.field(root, "targets", "targets");
    r.sequence(targets, "targets");
    for (std::size_t i = 0; i < targets.size(); ++i) {
      auto node = targets[i];
      const auto path = "targets[" + std::to_string(i) + "]";
      detail::target_fields(r, node, path);
      d.targets.push_back(
          {r.string(r.field(node, "name", path + ".name"), path + ".name"),
           r.string(r.field(node, "clock_domain", path + ".clock_domain"), path + ".clock_domain"),
           {}});
      d.targets.back().clock_gating = endpoint_gating(r, node, path);
      if (node["source_width"])
        d.targets.back().source_width = r.integer(
            r.field(node, "source_width", path + ".source_width"), path + ".source_width");
      if (node["size_width"])
        d.targets.back().size_width =
            r.integer(r.field(node, "size_width", path + ".size_width"), path + ".size_width");
      if (node["sink_width"])
        d.targets.back().sink_width =
            r.integer(r.field(node, "sink_width", path + ".sink_width"), path + ".sink_width");
      if (node["max_transfer_bytes"])
        d.targets.back().max_transfer_bytes =
            r.integer(r.field(node, "max_transfer_bytes", path + ".max_transfer_bytes"),
                      path + ".max_transfer_bytes");
      if (node["pipes"]) {
        const auto at = path + ".pipes";
        const auto item = r.field(node, "pipes", at);
        r.object(item, at, {"arrival", "dispatch", "response", "request"});
        auto &pipes = d.targets.back().pipes;
        pipeline(item, at, "arrival", pipes.arrival);
        pipeline(item, at, "response", pipes.response);
        pipeline(item, at, "request", pipes.request);
        if (item["dispatch"])
          pipes.dispatch = r.boolean(item["dispatch"], at + ".dispatch");
      }
      auto mappings = r.field(node, "mappings", path + ".mappings");
      r.sequence(mappings, path + ".mappings");
      for (std::size_t m = 0; m < mappings.size(); ++m) {
        const auto at = path + ".mappings[" + std::to_string(m) + "]";
        auto mapping = mappings[m];
        r.object(mapping, at, {"base", "size", "local_address", "when", "flow", "region"});
        auto &out = d.targets.back().mappings.emplace_back();
        out.base = r.address(r.field(mapping, "base", at + ".base"), at + ".base");
        out.size = r.address(r.field(mapping, "size", at + ".size"), at + ".size");
        if (mapping["local_address"])
          out.local_address = r.address(r.field(mapping, "local_address", at + ".local_address"),
                                        at + ".local_address");
        if (mapping["flow"])
          out.flow = r.integer(r.field(mapping, "flow", at + ".flow"), at + ".flow");
        if (mapping["region"])
          out.region = r.integer(r.field(mapping, "region", at + ".region"), at + ".region");
        if (mapping["when"]) {
          // A mode condition: {flag: true | false, ...}.
          const auto when = r.field(mapping, "when", at + ".when");
          if (!when.IsMap() || when.size() == 0)
            r.fail("NCL_SCHEMA_TYPE", at + ".when",
                   "Expected a nonempty object of address selector values", when.Mark());
          for (const auto &entry : when) {
            const auto flag = entry.first.Scalar();
            out.when[flag] =
                r.boolean(r.field(when, flag, at + ".when." + flag), at + ".when." + flag);
          }
        }
      }
      wishbone_fields(r, node, path, d.targets.back());
      if (node["ahb5"])
        d.targets.back().ahb5 = ahb5_properties(r, node["ahb5"], path + ".ahb5");
      if (node["axi5"])
        d.targets.back().axi5 = axi5_properties(r, node["axi5"], path + ".axi5");
      if (node["address_width"])
        d.targets.back().address_width = r.integer(
            r.field(node, "address_width", path + ".address_width"), path + ".address_width");
      if (node["access"])
        d.targets.back().access = access(node, path);
      for (auto [key, out] : {std::pair{"id_width", &d.targets.back().id_width},
                              std::pair{"len_width", &d.targets.back().len_width},
                              std::pair{"read_block_words", &d.targets.back().read_block_words}})
        if (node[key])
          *out = r.integer(r.field(node, key, path + "." + key), path + "." + key);
      if (node["read_interleaving_disabled"])
        d.targets.back().read_interleaving_disabled =
            r.boolean(node["read_interleaving_disabled"], path + ".read_interleaving_disabled");
      d.targets.back().data_width =
          r.integer(r.field(node, "data_width", path + ".data_width"), path + ".data_width");
      d.targets.back().protocol = endpoint_protocol(r, node, path + ".protocol", false);
      if (node["apb_back_to_back"])
        d.targets.back().apb_back_to_back =
            r.boolean(node["apb_back_to_back"], path + ".apb_back_to_back");
      if (node["axqos_levels"]) {
        const auto at = path + ".axqos_levels";
        const auto list = r.field(node, "axqos_levels", at);
        r.sequence(list, at);
        for (std::size_t k = 0; k < list.size(); ++k)
          d.targets.back().axqos_levels.push_back(
              r.integer(list[k], at + "[" + std::to_string(k) + "]"));
      }
      if (node["region_port"])
        d.targets.back().region_port = r.boolean(node["region_port"], path + ".region_port");
      if (node["read_side_effects"])
        d.targets.back().read_side_effects =
            r.boolean(node["read_side_effects"], path + ".read_side_effects");
      if (node["fixed_bursts"]) {
        const auto value = r.field(node, "fixed_bursts", path + ".fixed_bursts");
        const auto name = r.string(value, path + ".fixed_bursts");
        if (name == "reject")
          d.targets.back().fixed_bursts = FixedBursts::Reject;
        else if (name == "single")
          d.targets.back().fixed_bursts = FixedBursts::Single;
        else
          r.fail("NCL_TARGET_FIXED_BURSTS", path + ".fixed_bursts", "Expected reject or single",
                 value.Mark());
      }
      if (node["undefined_length_bursts"])
        d.targets.back().undefined_length_bursts =
            r.boolean(r.field(node, "undefined_length_bursts", path + ".undefined_length_bursts"),
                      path + ".undefined_length_bursts");
      if (node["hresp_width"])
        d.targets.back().hresp_width =
            r.integer(r.field(node, "hresp_width", path + ".hresp_width"), path + ".hresp_width");
      if (node["flows"])
        d.targets.back().flows =
            r.integer(r.field(node, "flows", path + ".flows"), path + ".flows");
      if (node["max_outstanding"])
        d.targets.back().max_outstanding = r.integer(
            r.field(node, "max_outstanding", path + ".max_outstanding"), path + ".max_outstanding");
      if (node["exclusive"]) {
        const auto value = r.field(node, "exclusive", path + ".exclusive");
        const auto name = r.string(value, path + ".exclusive");
        if (name == "none")
          d.targets.back().exclusive = ExclusiveAccess::None;
        else if (name == "network")
          d.targets.back().exclusive = ExclusiveAccess::Network;
        else if (name == "target")
          d.targets.back().exclusive = ExclusiveAccess::Target;
        else
          r.fail("NCL_TARGET_EXCLUSIVE", path + ".exclusive", "Expected none, network or target",
                 value.Mark());
      }
      if (node["exclusive_owner_flags"]) {
        const auto at = path + ".exclusive_owner_flags";
        const auto flags = r.field(node, "exclusive_owner_flags", at);
        r.sequence(flags, at);
        for (std::size_t k = 0; flags.IsSequence() && k < flags.size(); ++k)
          d.targets.back().exclusive_owner_flags.push_back(
              r.string(flags[k], at + "[" + std::to_string(k) + "]"));
      }
      if (const auto monitor = node["exclusive_monitor"]) {
        const auto at = path + ".exclusive_monitor";
        r.object(monitor, at, {"reservations", "granule_bytes"});
        ExclusiveMonitorOptions options;
        if (monitor["reservations"])
          options.reservations = r.integer(monitor["reservations"], at + ".reservations");
        if (monitor["granule_bytes"])
          options.granule_bytes = r.integer(monitor["granule_bytes"], at + ".granule_bytes");
        d.targets.back().exclusive_monitor = options;
      }
      if (node["timeout"]) {
        const auto value = r.field(node, "timeout", path + ".timeout");
        r.object(value, path + ".timeout", {"cycles", "completion"});
        TargetTimeout timeout{r.integer(r.field(value, "cycles", path + ".timeout.cycles"),
                                        path + ".timeout.cycles")};
        if (value["completion"])
          timeout.completion = r.boolean(value["completion"], path + ".timeout.completion");
        d.targets.back().timeout = timeout;
      }
      if (node["data_user_width"])
        d.targets.back().data_user_width = r.integer(
            r.field(node, "data_user_width", path + ".data_user_width"), path + ".data_user_width");
      if (node["response_user_width"])
        d.targets.back().response_user_width =
            r.integer(r.field(node, "response_user_width", path + ".response_user_width"),
                      path + ".response_user_width");
      if (node["user"]) {
        const auto at = path + ".user";
        const auto bits = r.field(node, "user", at);
        r.sequence(bits, at);
        for (std::size_t b = 0; b < bits.size(); ++b)
          d.targets.back().user.push_back(user_bit(r, bits[b], at + "[" + std::to_string(b) + "]"));
      }
      if (node["attributes"])
        d.targets.back().attributes = target_attributes(
            r, r.field(node, "attributes", path + ".attributes"), path + ".attributes",
            d.targets.back().protocol.value_or(Protocol::Axi4));
    }
    if (root["error_logging"])
      error_logging(r, root["error_logging"], d);
    auto output = r.field(root, "output", "output");
    r.object(output, "output", {"backend", "language", "register_reset"});
    for (auto [key, expected] :
         {std::pair{"backend", "verilog"}, std::pair{"language", "verilog_2001"}}) {
      auto path = std::string("output.") + key;
      auto value = r.field(output, key, path);
      if (r.string(value, path) != expected)
        r.fail("NCL_UNSUPPORTED_BACKEND", path, "Only the Verilog-2001 backend is implemented",
               value.Mark());
    }
    if (output["register_reset"]) {
      auto value = r.field(output, "register_reset", "output.register_reset");
      const auto kind = r.string(value, "output.register_reset");
      if (kind != "asynchronous" && kind != "synchronous")
        r.fail("NCL_SCHEMA_TYPE", "output.register_reset", "Expected asynchronous or synchronous",
               value.Mark());
      parsed.options.register_reset =
          kind == "synchronous" ? RegisterReset::Synchronous : RegisterReset::Asynchronous;
    }
    parsed.sources = std::move(r.sources);
    return Result<ParsedDefinition>::success(std::move(parsed));
  } catch (const ParseFailure &error) {
    return Result<ParsedDefinition>::failure(error.diagnostic);
  } catch (const YAML::Exception &error) {
    Diagnostic d{"NCL_SCHEMA_SYNTAX", "", error.msg, {}, {}};
    if (!error.mark.is_null())
      d.location =
          SourceLocation{std::string(source_label), static_cast<unsigned>(error.mark.line + 1),
                         static_cast<unsigned>(error.mark.column + 1)};
    return Result<ParsedDefinition>::failure(std::move(d));
  }
}
} // namespace nocloom::config
