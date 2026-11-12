// Shorthand forms of the YAML adapter. Each expands into the plain
// Definition that the written-out form gives, in a fixed order, so both
// forms generate the same bytes. The C++ API has no shorthand.
#include "reader.hpp"
#include <algorithm>
#include <iterator>
#include <map>
#include <set>
#include <type_traits>

namespace nocloom::config::detail {
namespace {
constexpr std::array<std::string_view, 22> initiator_keys{"name",
                                                          "clock_domain",
                                                          "address_width",
                                                          "protocol",
                                                          "data_width",
                                                          "id_width",
                                                          "len_width",
                                                          "access",
                                                          "narrow_bursts",
                                                          "max_outstanding",
                                                          "id_table_entries",
                                                          "qos",
                                                          "pipes",
                                                          "default_target",
                                                          "user_width",
                                                          "user_flags",
                                                          "audit_flags",
                                                          "response_user_width",
                                                          "route_classes",
                                                          "default_class",
                                                          "id_ranges",
                                                          "ahb5"};
constexpr std::array<std::string_view, 27> target_keys{"name",
                                                       "clock_domain",
                                                       "mappings",
                                                       "address_width",
                                                       "protocol",
                                                       "access",
                                                       "data_width",
                                                       "attributes",
                                                       "fixed_bursts",
                                                       "timeout",
                                                       "write_bursts",
                                                       "undefined_length_bursts",
                                                       "hresp_width",
                                                       "max_outstanding",
                                                       "exclusive",
                                                       "exclusive_monitors",
                                                       "exclusive_id_flags",
                                                       "apb_back_to_back",
                                                       "read_side_effects",
                                                       "id_width",
                                                       "len_width",
                                                       "read_interleave_bytes",
                                                       "pipes",
                                                       "user",
                                                       "response_user_width",
                                                       "flows",
                                                       "ahb5"};
constexpr std::array<const char *, 2> kinds{"initiators", "targets"};
constexpr std::array<const char *, 2> singular{"initiator", "target"};

std::string item(const std::string &path, std::size_t i) {
  return path + "[" + std::to_string(i) + "]";
}
// The endpoint names of a top-level list as written; malformed entries are
// left to the schema parser.
std::vector<std::string> names(const YAML::Node &root, const char *kind) {
  std::vector<std::string> result;
  if (const auto list = root[kind]; list.IsSequence())
    for (const auto &entry : list)
      if (entry.IsMap() && entry["name"].IsScalar())
        result.push_back(entry["name"].Scalar());
  return result;
}
bool contains(const std::vector<std::string> &list, const std::string &name) {
  return std::find(list.begin(), list.end(), name) != list.end();
}
// Names with the position each one is written at.
using Named = std::vector<std::pair<std::string, YAML::Mark>>;
// Appends each name of a sequence node (or a list) at one position.
template <typename List> void append(Named &out, const List &list, const YAML::Mark &mark) {
  std::transform(list.begin(), list.end(), std::back_inserter(out), [&](const auto &item) {
    if constexpr (std::is_same_v<std::decay_t<decltype(item)>, std::string>)
      return std::pair{item, mark};
    else
      return std::pair{item.Scalar(), mark};
  });
}
} // namespace

std::span<const std::string_view> initiator_fields() { return initiator_keys; }
std::span<const std::string_view> target_fields() { return target_keys; }

void pipeline(Reader &r, const YAML::Node &item, const std::string &field, const char *name,
              LinkPipeline &out) {
  if (!item[name])
    return;
  const auto at = field + "." + name;
  const auto pipe = r.field(item, name, at);
  r.object(pipe, at, {"forward", "backward", "half_rate", "bypass_port", "ungated"});
  for (auto [key, flag] :
       {std::pair{"forward", &out.forward}, std::pair{"backward", &out.backward},
        std::pair{"half_rate", &out.half_rate}, std::pair{"ungated", &out.ungated}})
    if (pipe[key])
      *flag = r.boolean(pipe[key], at + "." + key);
  if (pipe["bypass_port"])
    out.bypass_port = r.string(pipe["bypass_port"], at + ".bypass_port");
}

// templates: {initiators: {<name>: {fields}}, targets: {...}}. An entry's
// `use` names one template or a list; the entry takes every field of each.
// No field may come from two places: the templates of one entry set
// disjoint fields, and an entry sets none of them itself.
void expand_templates(Reader &r, const YAML::Node &root) {
  struct Template {
    std::string name;
    YAML::Node body;
    bool used = false;
  };
  std::array<std::vector<Template>, 2> templates;
  if (root["templates"]) {
    const auto node = r.field(root, "templates", "templates");
    r.object(node, "templates", {"initiators", "targets"});
    for (std::size_t k = 0; k < kinds.size(); ++k) {
      if (!node[kinds[k]])
        continue;
      const auto path = std::string("templates.") + kinds[k];
      const auto list = r.field(node, kinds[k], path);
      if (!list.IsMap() || list.size() == 0)
        r.fail("NCL_SCHEMA_TYPE", path, "Expected a nonempty object of named templates",
               list.Mark());
      for (const auto &entry : list) {
        const auto at = path + "." + entry.first.Scalar();
        const auto &body = entry.second;
        if (!body.IsMap() || body.size() == 0)
          r.fail("NCL_TEMPLATE_FIELD", at, "A template is an object that sets at least one field",
                 body.Mark());
        for (const auto &field : body)
          if (field.first.Scalar() == "name" || field.first.Scalar() == "use")
            r.fail("NCL_TEMPLATE_FIELD", at + "." + field.first.Scalar(),
                   "A template sets neither name nor use", field.first.Mark());
        r.object(body, at, k == 0 ? initiator_fields() : target_fields());
        templates[k].push_back({entry.first.Scalar(), body});
      }
    }
  }
  for (std::size_t k = 0; k < kinds.size(); ++k) {
    const auto list = root[kinds[k]];
    if (!list.IsSequence())
      continue;
    for (std::size_t i = 0; i < list.size(); ++i) {
      YAML::Node entry = list[i];
      const YAML::Node &view = entry;
      if (!view.IsMap() || !view["use"])
        continue;
      const auto path = item(kinds[k], i);
      const auto use = view["use"];
      std::vector<std::pair<YAML::Node, std::string>> named;
      if (use.IsSequence()) {
        if (use.size() == 0)
          r.fail("NCL_SCHEMA_TYPE", path + ".use",
                 "Expected a template name or a nonempty list of template names", use.Mark());
        for (std::size_t j = 0; j < use.size(); ++j)
          named.emplace_back(use[j], item(path + ".use", j));
      } else {
        named.emplace_back(use, path + ".use");
      }
      std::vector<Template *> chosen;
      std::map<std::string, std::string> owner;
      for (const auto &[node, at] : named) {
        const auto name = r.string(node, at);
        auto found = std::find_if(templates[k].begin(), templates[k].end(),
                                  [&](const Template &t) { return t.name == name; });
        if (found == templates[k].end()) {
          const auto &other = templates[1 - k];
          const bool elsewhere = std::any_of(other.begin(), other.end(),
                                             [&](const Template &t) { return t.name == name; });
          r.fail("NCL_TEMPLATE_REFERENCE", at,
                 std::string("No ") + singular[k] + " template " + name +
                     (elsewhere ? std::string(" (it is a ") + singular[1 - k] + " template)" : ""),
                 node.Mark());
        }
        if (std::find(chosen.begin(), chosen.end(), &*found) != chosen.end())
          r.fail("NCL_TEMPLATE_CONFLICT", at, "Template " + name + " is named twice", node.Mark());
        for (const auto &field : found->body) {
          const auto key = field.first.Scalar();
          if (view[key])
            r.fail("NCL_TEMPLATE_CONFLICT", path + "." + key,
                   "Field " + key + " is set by the entry and by template " + name +
                       "; an entry does not override its templates",
                   view[key].Mark());
          if (const auto [it, fresh] = owner.emplace(key, name); !fresh)
            r.fail("NCL_TEMPLATE_CONFLICT", path + "." + key,
                   "Field " + key + " is set by templates " + it->second + " and " + name,
                   node.Mark());
        }
        found->used = true;
        chosen.push_back(&*found);
      }
      entry.remove("use");
      for (const auto *t : chosen)
        for (const auto &field : t->body)
          entry[field.first.Scalar()] = field.second;
    }
  }
  for (std::size_t k = 0; k < kinds.size(); ++k)
    for (const auto &t : templates[k])
      if (!t.used)
        r.fail("NCL_TEMPLATE_UNUSED", std::string("templates.") + kinds[k] + "." + t.name,
               std::string("No ") + singular[k] + " uses template " + t.name, t.body.Mark());
}

// groups: {initiators: {<name>: [members]}, targets: {...}}. A connectivity
// row names an initiator or an initiator group and maps it to all, to
// {all_except: [...]} or to a list of targets and target groups. Expansion:
// rows in written order, the members of a group in group order, all in the
// order of targets.
Connectivity connectivity(Reader &r, const YAML::Node &root) {
  const std::array<std::vector<std::string>, 2> endpoints{names(root, "initiators"),
                                                          names(root, "targets")};
  struct Group {
    std::size_t kind = 0;
    YAML::Node members;
    YAML::Mark mark;
    bool used = false;
  };
  std::map<std::string, Group> groups;
  std::vector<std::string> order;
  if (root["groups"]) {
    const auto node = r.field(root, "groups", "groups");
    r.object(node, "groups", {"initiators", "targets"});
    for (std::size_t k = 0; k < kinds.size(); ++k) {
      if (!node[kinds[k]])
        continue;
      const auto path = std::string("groups.") + kinds[k];
      const auto list = r.field(node, kinds[k], path);
      if (!list.IsMap() || list.size() == 0)
        r.fail("NCL_SCHEMA_TYPE", path, "Expected a nonempty object of named groups", list.Mark());
      for (const auto &entry : list) {
        const auto name = entry.first.Scalar();
        const auto at = path + "." + name;
        if (contains(endpoints[0], name) || contains(endpoints[1], name))
          r.fail("NCL_GROUP_NAME", at, "Group " + name + " has the name of an endpoint",
                 entry.first.Mark());
        if (groups.contains(name))
          r.fail("NCL_GROUP_NAME", at, "Group " + name + " is also an initiator group",
                 entry.first.Mark());
        const auto &members = entry.second;
        r.sequence(members, at);
        if (members.size() == 0)
          r.fail("NCL_SCHEMA_TYPE", at, "Expected a nonempty list of members", members.Mark());
        std::set<std::string> seen;
        for (std::size_t j = 0; j < members.size(); ++j) {
          const auto member = r.string(members[j], item(at, j));
          if (!contains(endpoints[k], member))
            r.fail("NCL_GROUP_REFERENCE", item(at, j),
                   std::string("Unknown ") + singular[k] + " " + member, members[j].Mark());
          if (!seen.insert(member).second)
            r.fail("NCL_GROUP_DUPLICATE", item(at, j), member + " is listed twice in group " + name,
                   members[j].Mark());
        }
        groups.emplace(name, Group{k, members, entry.first.Mark()});
        order.push_back(name);
      }
    }
  }
  const auto matrix = r.field(root, "connectivity", "connectivity");
  Connectivity result;
  if (matrix.IsScalar()) {
    if (r.string(matrix, "connectivity") != "all")
      r.fail("NCL_SCHEMA_TYPE", "connectivity",
             "Expected all or a map from each source to a list of targets", matrix.Mark());
    result.all = true;
  } else {
    if (!matrix.IsMap())
      r.fail("NCL_SCHEMA_TYPE", "connectivity",
             "Expected all or a map from each source to a list of targets", matrix.Mark());
    // A target name or a target group, with the mark of where it is written.
    const auto expand = [&](const YAML::Node &node, const std::string &at, Named &out) {
      const auto name = r.string(node, at);
      const auto group = groups.find(name);
      if (group == groups.end()) {
        out.emplace_back(name, node.Mark());
        return;
      }
      if (group->second.kind == 0)
        r.fail("NCL_CONNECTIVITY_REFERENCE", at,
               name + " is an initiator group; a row lists targets and target groups", node.Mark());
      group->second.used = true;
      append(out, group->second.members, node.Mark());
    };
    std::map<std::string, std::string> row_of;
    for (const auto &row : matrix) {
      const auto key = row.first.Scalar();
      const auto path = "connectivity." + key;
      const auto value = r.field(matrix, key, path);
      Named sources;
      if (const auto group = groups.find(key); group != groups.end()) {
        if (group->second.kind == 1)
          r.fail("NCL_CONNECTIVITY_REFERENCE", path,
                 key + " is a target group; a row names an initiator or an initiator group",
                 row.first.Mark());
        group->second.used = true;
        append(sources, group->second.members, value.Mark());
      } else {
        sources.emplace_back(key, value.Mark());
      }
      Named targets;
      std::vector<std::pair<std::optional<unsigned>, std::optional<unsigned>>> flows;
      if (value.IsScalar() && value.Scalar() == "all") {
        append(targets, endpoints[1], value.Mark());
      } else if (value.IsMap()) {
        r.object(value, path, {"all_except"});
        const auto at = path + ".all_except";
        const auto except = r.field(value, "all_except", at);
        r.sequence(except, at);
        Named excluded;
        for (std::size_t j = 0; j < except.size(); ++j) {
          const auto before = excluded.size();
          expand(except[j], item(at, j), excluded);
          for (auto x = before; x < excluded.size(); ++x) {
            if (!contains(endpoints[1], excluded[x].first))
              r.fail("NCL_CONNECTIVITY_REFERENCE", item(at, j),
                     "Unknown target " + excluded[x].first, except[j].Mark());
            for (std::size_t y = 0; y < x; ++y)
              if (excluded[y].first == excluded[x].first)
                r.fail("NCL_CONNECTIVITY_DUPLICATE", item(at, j),
                       "Target " + excluded[x].first + " is excluded twice", except[j].Mark());
          }
        }
        for (const auto &t : endpoints[1])
          if (std::none_of(excluded.begin(), excluded.end(),
                           [&](const auto &x) { return x.first == t; }))
            targets.emplace_back(t, value.Mark());
      } else {
        r.sequence(value, path);
        for (std::size_t i = 0; i < value.size(); ++i) {
          const auto at = item(path, i);
          if (!value[i].IsMap()) {
            expand(value[i], at, targets);
            continue;
          }
          // One target's flow pairs: {target, source_path, target_path}, a flow
          // absent for every flow of its side.
          r.object(value[i], at, {"target", "source_path", "target_path"});
          targets.emplace_back(
              r.string(r.field(value[i], "target", at + ".target"), at + ".target"),
              value[i].Mark());
          flows.resize(targets.size());
          for (const auto *side : {"source_path", "target_path"})
            if (value[i][side])
              (std::string_view(side) == "source_path" ? flows.back().first : flows.back().second) =
                  r.integer(r.field(value[i], side, at + "." + side), at + "." + side);
        }
      }
      flows.resize(targets.size());
      for (const auto &[source, mark] : sources) {
        if (const auto [it, fresh] = row_of.emplace(source, key); !fresh)
          r.fail("NCL_CONNECTIVITY_OVERLAP", path,
                 "Initiator " + source + " is in rows " + it->second + " and " + key +
                     "; each initiator takes one row",
                 row.first.Mark());
        r.note("connectivity." + source, mark);
        for (std::size_t i = 0; i < targets.size(); ++i) {
          r.note(item("connectivity." + source, i), targets[i].second);
          result.pairs.push_back({source, targets[i].first, flows[i].first, flows[i].second});
        }
      }
    }
  }
  for (const auto &name : order)
    if (const auto &group = groups.at(name); !group.used)
      r.fail("NCL_GROUP_UNUSED", std::string("groups.") + kinds[group.kind] + "." + name,
             "No connectivity row uses group " + name, group.mark);
  return result;
}

NodePipes node_pipes(Reader &r, const YAML::Node &item, const std::string &field) {
  NodePipes result;
  for (std::size_t side = 0; side < 2; ++side) {
    const char *key = side == 0 ? "output_links" : "input_links";
    if (!item[key])
      continue;
    const auto at = field + "." + key;
    const auto node = r.field(item, key, at);
    r.object(node, at, {"pipeline", "sender_pipeline"});
    if (node.size() == 0)
      r.fail("NCL_NODE_PIPE_UNUSED", at, "Node links default sets no pipe", node.Mark());
    for (std::size_t f = 0; f < 2; ++f) {
      const char *name = f == 0 ? "pipeline" : "sender_pipeline";
      if (!node[name])
        continue;
      LinkPipeline pipe;
      pipeline(r, node, at, name, pipe);
      result.pipes[side * 2 + f] = pipe;
      result.marks[side * 2 + f] = node[name].Mark();
    }
  }
  return result;
}

void apply_node_pipes(const Reader &r, const std::string &path,
                      const std::vector<NodePipes> &defaults, const std::vector<LinkPipes> &links,
                      ExchangeGraph &graph) {
  std::map<std::string, std::size_t> index;
  for (std::size_t i = 0; i < graph.nodes.size(); ++i)
    index.emplace(graph.nodes[i].name, i);
  std::vector<std::array<bool, 4>> used(defaults.size());
  for (std::size_t j = 0; j < graph.links.size(); ++j) {
    auto &link = graph.links[j];
    const auto from = index.find(link.from);
    const auto to = index.find(link.to);
    for (std::size_t f = 0; f < 2; ++f) {
      if (f == 0 ? links[j].pipeline : links[j].sender_pipeline)
        continue;
      const auto *out = from == index.end() ? nullptr : &defaults[from->second].pipes[f];
      const auto *in = to == index.end() ? nullptr : &defaults[to->second].pipes[2 + f];
      const bool by_sender = out && out->has_value();
      const bool by_receiver = in && in->has_value();
      const char *name = f == 0 ? "pipeline" : "sender_pipeline";
      if (by_sender && by_receiver)
        r.fail("NCL_NODE_PIPE_CONFLICT", item(path + ".links", j),
               "Link " + link.from + " -> " + link.to + " takes its " + name + " from the " +
                   "output_links of " + link.from + " and the input_links of " + link.to +
                   "; set it on the link",
               links[j].mark);
      if (by_sender) {
        (f == 0 ? link.pipeline : link.sender_pipeline) = **out;
        used[from->second][f] = true;
      } else if (by_receiver) {
        (f == 0 ? link.pipeline : link.sender_pipeline) = **in;
        used[to->second][2 + f] = true;
      }
    }
  }
  for (std::size_t i = 0; i < defaults.size(); ++i)
    for (std::size_t slot = 0; slot < 4; ++slot)
      if (defaults[i].pipes[slot] && !used[i][slot])
        r.fail("NCL_NODE_PIPE_UNUSED",
               item(path + ".nodes", i) + (slot < 2 ? ".output_links." : ".input_links.") +
                   (slot % 2 == 0 ? "pipeline" : "sender_pipeline"),
               "No link takes this default: node " + graph.nodes[i].name +
                   " has no such link, or each one sets the pipe itself",
               defaults[i].marks[slot]);
}

// response: mirror, or {mirror: {rename_suffix, link_overrides}}. Nodes keep
// the request order, name, transport_word_bytes and clock_domain (no
// arbitration); the i-th link reverses the i-th request link and swaps
// pipeline with sender_pipeline; entries are the request exits and exits the
// request entries, each with its pipeline. Overrides then replace the fields
// they set on one mirrored link.
ExchangeGraph mirror(Reader &r, const ExchangeGraph &request, const YAML::Node &spec,
                     const std::string &path) {
  std::optional<std::pair<std::string, std::string>> suffix;
  YAML::Mark suffix_mark;
  YAML::Node overrides;
  const auto options = path + ".mirror";
  if (spec.IsMap()) {
    r.object(spec, path, {"mirror"});
    const auto node = r.field(spec, "mirror", options);
    r.object(node, options, {"rename_suffix", "link_overrides"});
    if (node["rename_suffix"]) {
      const auto at = options + ".rename_suffix";
      const auto rename = r.field(node, "rename_suffix", at);
      r.object(rename, at, {"from", "to"});
      suffix = {r.string(r.field(rename, "from", at + ".from"), at + ".from"),
                r.string(r.field(rename, "to", at + ".to"), at + ".to")};
      suffix_mark = rename.Mark();
    }
    if (node["link_overrides"]) {
      overrides = r.field(node, "link_overrides", options + ".link_overrides");
      r.sequence(overrides, options + ".link_overrides");
    }
  }
  std::map<std::string, std::string> renamed;
  for (const auto &n : request.nodes) {
    auto name = n.name;
    if (suffix) {
      if (!name.ends_with(suffix->first))
        r.fail("NCL_MIRROR_RENAME", options + ".rename_suffix",
               "Request node " + name + " does not end with " + suffix->first, suffix_mark);
      name = name.substr(0, name.size() - suffix->first.size()) + suffix->second;
    }
    renamed.emplace(n.name, name);
  }
  const auto rename = [&](const std::string &name) {
    const auto it = renamed.find(name);
    return it == renamed.end() ? name : it->second;
  };
  ExchangeGraph g;
  for (const auto &n : request.nodes)
    g.nodes.push_back({rename(n.name), n.transport_word_bytes, n.clock_domain, {}});
  for (const auto &l : request.links)
    g.links.push_back(
        {rename(l.to), rename(l.from), l.transport_word_bytes, l.sender_pipeline, l.pipeline});
  for (const auto &a : request.exits)
    g.entries.push_back({a.endpoint, rename(a.node), a.pipeline});
  for (const auto &a : request.entries)
    g.exits.push_back({a.endpoint, rename(a.node), a.pipeline});
  std::set<std::size_t> touched;
  for (std::size_t i = 0; overrides && i < overrides.size(); ++i) {
    const auto at = item(options + ".link_overrides", i);
    const auto entry = overrides[i];
    r.object(entry, at, {"from", "to", "transport_word_bytes", "pipeline", "sender_pipeline"});
    const auto from = r.string(r.field(entry, "from", at + ".from"), at + ".from");
    const auto to = r.string(r.field(entry, "to", at + ".to"), at + ".to");
    const auto found = std::find_if(g.links.begin(), g.links.end(), [&](const ExchangeLink &l) {
      return l.from == from && l.to == to;
    });
    if (found == g.links.end())
      r.fail("NCL_MIRROR_OVERRIDE", at, "The mirrored graph has no link " + from + " -> " + to,
             entry.Mark());
    if (!touched.insert(static_cast<std::size_t>(found - g.links.begin())).second)
      r.fail("NCL_MIRROR_OVERRIDE", at, "Link " + from + " -> " + to + " is overridden twice",
             entry.Mark());
    if (!entry["transport_word_bytes"] && !entry["pipeline"] && !entry["sender_pipeline"])
      r.fail("NCL_MIRROR_OVERRIDE", at,
             "An override sets transport_word_bytes, pipeline or sender_pipeline", entry.Mark());
    auto link = *found;
    if (entry["transport_word_bytes"])
      link.transport_word_bytes =
          r.integer(r.field(entry, "transport_word_bytes", at + ".transport_word_bytes"),
                    at + ".transport_word_bytes");
    if (entry["pipeline"]) {
      link.pipeline = {};
      pipeline(r, entry, at, "pipeline", link.pipeline);
    }
    if (entry["sender_pipeline"]) {
      link.sender_pipeline = {};
      pipeline(r, entry, at, "sender_pipeline", link.sender_pipeline);
    }
    if (link == *found)
      r.fail("NCL_MIRROR_OVERRIDE", at, "The override repeats the mirrored link", entry.Mark());
    *found = link;
  }
  return g;
}
} // namespace nocloom::config::detail
