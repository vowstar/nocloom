#include "axi4_output.hpp"
#include "backends/output_license.hpp"
#include "diagnostic.hpp"
#include "network_plan_profiles.hpp"
#include <algorithm>
#include <bit>
#include <functional>
#include <iterator>
#include <nlohmann/json.hpp>
#include <ranges>
#include <span>

namespace nocloom::detail {
namespace {
using Json = nlohmann::json;
Json flow_indices(const std::map<std::string, unsigned> &first,
                  const std::map<std::string, unsigned> &counts) {
  Json out = Json::object();
  for (const auto &[name, start] : first) {
    auto row = Json::array();
    for (unsigned f = 0; f < counts.at(name); ++f)
      row.push_back(start + f);
    out[name] = std::move(row);
  }
  return out;
}
Json route_fields(const TargetIdNumbering &ids, bool record = false) {
  auto out = Json::array();
  for (const auto &field : ids.fields(record))
    if (field.bits)
      out.push_back({{"name", field.name}, {"lsb", field.lsb}, {"width", field.bits}});
  return out;
}
// The records with a value, as a JSON object by name.
template <class Records, class Value> Json by_name(const Records &records, const Value &value) {
  auto out = Json::object();
  for (const auto &r : records)
    if (const auto v = value(r))
      out[r.name] = *v;
  return out;
}
// The names of the records that keep, in name order.
template <class Records, class Keep>
std::vector<std::string> names_if(const Records &records, Keep keep) {
  std::vector<std::string> out;
  for (const auto &r : records)
    if (std::invoke(keep, r))
      out.push_back(r.name);
  return out;
}
template <class Records, class Field> Json field_by_name(const Records &records, Field field) {
  return by_name(records, [&](const auto &r) { return std::optional{std::invoke(field, r)}; });
}
template <class Records, class Keep> bool any(const Records &records, Keep keep) {
  return std::ranges::any_of(records, keep);
}
bool has_exclusive(const TargetPlan &t) { return t.exclusive.mode != ExclusiveAccess::None; }
std::string value_ref(ValueId id) { return "value/" + std::to_string(id.index); }
Json value_refs(std::span<const ValueId> values) {
  const auto refs = values | std::views::transform(value_ref);
  return Json(std::vector<std::string>(refs.begin(), refs.end()));
}
constexpr const char *serial_pieces =
    "tag_packet_contexts_single_piece_overlap_multi_piece_or_local_error_drains_prior_responses_"
    "and_excludes_later_packets_four_serial_pieces_in_flight";
constexpr const char *fixed_reject = "SLVERR_every_beat_no_target_access";
constexpr const char *lane_read =
    "word_aligned_AxSIZE_lane_mask_no_coalescing_error_data_zero_continue";
constexpr const char *direction_capacity =
    "independent_direction_burst_owners_shared_bus_completion_grant";
constexpr const char *strobeless_write =
    "full_word_strobe_written_zero_strobe_skipped_partial_word_SLVERR_no_access_rest_of_packet_"
    "drained_no_access_next_packet_served";
// The address contract of targets with sub-page windows or non-32-bit addresses.
Json address_contract() {
  return {{"decode", "full_transport_address_then_subtract_base_then_narrow_to_local_port"},
          {"window", "4KiB_aligned_base_size_4KiB_multiple_or_power_of_two_below_4KiB"},
          {"subpage_remainder", "each_beat_DECERR_no_target_access_read_data_zero"},
          {"routing", "whole_4KiB_page_one_window_per_page"}};
}
template <class Bits> Json attribute_bits(const Bits &bits) {
  static const char *const text[] = {"0",      "1",          "bufferable", "modifiable", "allocate",
                                     "lookup", "privileged", "nonsecure",  "instruction"};
  Json out = Json::array();
  std::ranges::transform(bits, std::back_inserter(out), [](const auto &bit) {
    return (bit.invert ? "~" : "") + std::string(text[static_cast<int>(bit.source)]);
  });
  return out;
}
// Test ports, gating and the clock division appear only when stated.
void domain_gating(Json &record, const DomainPlan &domain) {
  if (!domain.test_port.empty())
    record["test_port"] = domain.test_port;
  if (!domain.gated)
    record["gated"] = false;
  if (domain.type != ClockType::Direct)
    record["clock_type"] = std::array{"direct", "external_divider", "internal_divider",
                                      "edge_qualified"}[static_cast<int>(domain.type)];
  if (domain.type == ClockType::InternalDivider)
    record["ratio"] = domain.ratio;
  if (!domain.enable_port.empty())
    record["enable_port"] = domain.enable_port;
  if (!domain.ungate_port.empty())
    record["ungate_port"] = domain.ungate_port;
  if (!domain.clock_root.empty())
    record["clock_root"] = domain.clock_root;
  if (!domain.edge_parent.empty())
    record["edge_parent"] = domain.edge_parent;
}
// Every DECERR text of a contract reads SLVERR.
void decerr_as_slverr(Json &node) {
  if (node.is_object() || node.is_array()) {
    for (auto &child : node)
      decerr_as_slverr(child);
  } else if (node.is_string()) {
    auto text = node.get<std::string>();
    for (auto at = text.find("DECERR"); at != std::string::npos; at = text.find("DECERR", at))
      text.replace(at, 6, "SLVERR");
    node = text;
  }
}
// Whether the map names a value the RTL does not contain (see canonicalize).
bool dangling(const Json &node, std::size_t emitted) {
  if (node.is_structured())
    return std::any_of(node.begin(), node.end(),
                       [&](const Json &child) { return dangling(child, emitted); });
  if (!node.is_string())
    return false;
  const auto &text = node.get_ref<const std::string &>();
  for (const std::string_view prefix : {"value/", "nl_internal_v"})
    if (text.starts_with(prefix) && text.size() > prefix.size() &&
        text.find_first_not_of("0123456789", prefix.size()) == std::string::npos)
      return std::stoull(text.substr(prefix.size())) >= emitted;
  return false;
}
// The arbitration contract: the default, the effective table when the default
// goes by candidate count, and every arbitrated physical output with its
// candidates in hardware index order, its policy and its service premise.
Json arbitration_output(const NetworkPlan &p, const ArbiterPlan &a) {
  const auto n = static_cast<unsigned>(a.candidates.size());
  const auto service = treadle::service(a.spec.policy, n);
  const auto label = [&](const std::string &key) { return p.label(key); };
  Json candidates = Json::array();
  std::ranges::transform(a.candidates, std::back_inserter(candidates), label);
  Json entry = {{"graph", a.response ? "response" : "request"},
                {"node", label("node/" + a.node).substr(5)},
                {"output", label(a.output)},
                {"candidates", candidates},
                {"policy", treadle::policy_name(a.spec.policy)},
                {"set_by", a.overridden ? "output" : "default"}};
  if (a.spec.policy == treadle::Policy::FixedPriority) {
    Json order = Json::array();
    for (unsigned r = 0; r < n; ++r)
      for (unsigned i = 0; i < n; ++i)
        if (a.spec.rank[i] == r)
          order.push_back(label(a.candidates[i]));
    entry["priority"] = order;
  }
  entry["service"] =
      p.levels() ? "highest_level_candidates_only_lower_levels_may_starve_"
                   "same_level_uses_the_selected_policy"
      : service.structural
          ? "bounded_wait_at_most_" + std::to_string(service.bound) + "_releases"
          : std::string("lower_priority_served_when_higher_priority_candidates_yield");
  return entry;
}
Json arbitration_contract(const NetworkPlan &p) {
  Json contract = {{"granularity", "packet_grant_held_from_head_to_tail_transfer"},
                   {"channels", "read_and_write_packets_share_each_output_arbiter"}};
  contract["default"] = treadle::policy_name(p.arbitration.choose(0));
  Json outputs = Json::array();
  bool environment = false;
  for (const auto &[key, a] : p.arbiters) {
    const auto n = static_cast<unsigned>(a.candidates.size());
    environment = environment || !treadle::service(a.spec.policy, n).structural;
    outputs.push_back(arbitration_output(p, a));
  }
  environment = environment || (p.levels() && !p.arbiters.empty());
  contract["outputs"] = std::move(outputs);
  contract["service"] = environment ? "fixed_priority_outputs_assume_higher_priority_candidates_"
                                      "eventually_yield"
                                    : "every_output_bounded_wait";
  const auto limited = [](const SourcePlan &s) { return s.qos.holds(); };
  if (!p.levels() && std::ranges::none_of(p.sources, limited))
    return contract;
  if (p.levels())
    contract["service"] = "lower_levels_require_higher_levels_to_yield_no_global_wait_bound";
  contract["qos"] = {{"selection", "highest_valid_pressure_before_unheld_choice"},
                     {"holding", "grant_kept_through_tail_transfer"},
                     {"fifo", "arrival_order_records_unfiltered_requests"},
                     {"physical_AxQOS",
                      p.mapped() ? "AWQOS_ARQOS_of_mapped_sources_only" : "absent_at_every_port"}};
  if (p.level_hint_bits())
    contract["qos"]["pressure"] = {
        {"source", "packet_head_level_and_regulator_levels"},
        {"switch_output", "or_of_its_input_pressures_while_valid"},
        {"pipe", "held_head_level_and_upstream_pressure_held_back"},
        {"width_adapter", "serializer_passes_input_pressure_deserializer_adds_output_head_level"},
        {"response_level", "context_level_when_first_offered_kept_until_taken"},
        {"crossing", "synchronized_pressure_and_head_levels_of_written_slots"},
        {"target_response", "levels_of_contexts_in_use_and_requests_held_back_a_cycle_later"}};
  if (p.mapped())
    contract["qos"]["mapped_levels"] = "request_packet_level_field";
  for (const auto &s : p.sources) {
    if (s.qos == InitiatorQos{})
      continue;
    auto &source = contract["qos"]["sources"][s.name];
    source = {{"levels", {{"read", s.qos.levels.read}, {"write", s.qos.levels.write}}}};
    if (!s.qos.axqos_map.empty())
      source["axqos_map"] = s.qos.axqos_map;
    if (const auto &l = s.qos.shaper)
      source["shaper"] = {{"bytes_per_1024_cycles", l->bytes_per_1024_cycles},
                          {"burst_bytes", l->burst_bytes},
                          {"effect", s.qos.picks() ? "picks_the_scheduling_level"
                                                   : "holds_new_transfers_over_budget"}};
    if (const auto &g = s.qos.scheduler)
      source["scheduler"] = {{"under_level", g->under_level},
                             {"over_level", g->over_level},
                             {"threshold_percent", g->threshold_percent},
                             {"packet_level", "level_when_first_offered_kept_until_taken"}};
  }
  return contract;
}
// Formal observation anchors: map key and binding suffix under the endpoint.
using Anchor = std::pair<const char *, const char *>;
constexpr Anchor source_anchors[] = {{"write_miss", "wr.missing"},
                                     {"read_miss", "rr.missing"},
                                     {"write_state", "wr.state"},
                                     {"write_desc_addr", "wr.desc.addr"},
                                     {"read_state", "rr.state"},
                                     {"read_desc_addr", "rr.desc.addr"},
                                     {"write_staged_resp", "wr.bresp"},
                                     {"read_staged_full", "rr.rsp_full"},
                                     {"read_staged_last", "rr.rsp_last"},
                                     {"read_desc_id", "rr.desc.id"},
                                     {"read_staged_result", "rr.rsp_result"},
                                     {"read_staged_error", "rr.rsp_error"},
                                     {"write_return_valid", "write.return_valid"},
                                     {"write_return_result", "write.return_result"},
                                     {"write_return_error", "write.return_error"},
                                     {"read_return_valid", "read.return_valid"},
                                     {"read_return_result", "read.return_result"},
                                     {"read_return_error", "read.return_error"},
                                     {"request_valid", "request.packet_valid"},
                                     {"request_ready", "request.packet_ready"},
                                     {"request_write", "request.packet_write"},
                                     {"request_error", "request.packet_error"},
                                     {"request_pool", "request.pool"},
                                     {"request_raise", "request.packet_raise"},
                                     {"scheduler_counter", "regulator.counter"},
                                     {"scheduler_charged", "regulator.charged"},
                                     {"scheduler_keep", "regulator.keep"},
                                     {"scheduler_held", "regulator.held"},
                                     {"scheduler_valid", "regulator.control_valid"},
                                     {"scheduler_level", "regulator.control_level"},
                                     {"request_level", "request.packet_level"},
                                     {"pre_merge_write_valid", "request.pre_merge_write_valid"},
                                     {"pre_merge_write_error", "request.pre_merge_write_error"},
                                     {"pre_merge_read_valid", "request.pre_merge_read_valid"}};
constexpr Anchor target_anchors[] = {{"write_busy", "w.busy"},
                                     {"write_miss", "w.miss"},
                                     {"write_response", "w.response"},
                                     {"write_response_full", "w.response_full"},
                                     {"read_busy", "r.busy"},
                                     {"read_miss", "r.miss"},
                                     {"read_response", "r.response"},
                                     {"read_response_full", "r.full"},
                                     {"offer_valid", "offer_valid"},
                                     {"offer_write", "offer_write"},
                                     {"offer_error", "offer_error"},
                                     {"emit_valid", "emit_valid"},
                                     {"emit_result", "emit_result"},
                                     {"emit_route", "emit_route"},
                                     {"write_packet_user", "w.packet_user"},
                                     {"write_response_user", "w.response.user"}};
// The map and bundle of one composed network in named stages. Stage order matters: later
// stages refine contract texts (attributes, target_response, fixed) of earlier ones.
class MapWriter {
public:
  MapWriter(const NetworkPlan &network, const ExchangeHardware &hardware)
      : p(network), h(hardware), topology(Json::parse(network.topology_record)) {}
  Result<ArtifactSet> run() {
    for (auto stage :
         {&MapWriter::network_record, &MapWriter::endpoint_transactions, &MapWriter::target_widths,
          &MapWriter::target_attributes, &MapWriter::user_flags, &MapWriter::packets,
          &MapWriter::error_responses, &MapWriter::routing, &MapWriter::width_conversion,
          &MapWriter::ingress, &MapWriter::target_contracts, &MapWriter::target_options,
          &MapWriter::exclusive_access, &MapWriter::outstanding, &MapWriter::target_id_record,
          &MapWriter::error_encoding})
      (this->*stage)();
    if (h.plan.clocked())
      clock_record();
    auto map = map_record();
    if (h.plan.clocked())
      clocked_network(map["network"]);
    clock_gates(map["network"], map["definition"]);
    if (p.cells.clock_gate || p.cells.synchronizer)
      cells(map);
    if (dangling(map, h.ir.emitted.value_or(h.ir.values.size())))
      return Result<ArtifactSet>::failure(
          error("NCL_INTERNAL_MAP", "output.map", "The map names logic the RTL does not contain"));
    return Result<ArtifactSet>::success(artifacts(map));
  }

private:
  const NetworkPlan &p;
  const ExchangeHardware &h;
  const Json topology;
  const bool multi_source = any(p.sources, [](const auto &s) { return s.outstanding > 1; });
  const bool multi_target = any(p.targets, [](const auto &t) { return t.outstanding > 1; });
  const Json apb_widths = by_name(p.targets, [](const auto &t) {
    return t.apb_bits < 32 ? std::optional{t.apb_bits} : std::nullopt;
  });
  // APB initiators travel as AXI4-Lite sources, AHB and AXI3 initiators as AXI4 sources; their
  // own protocol names them.
  const Json source_protocols = by_name(p.sources, [](const auto &s) {
    return std::optional{s.apb                              ? *s.apb == Protocol::Apb3   ? "apb3"
                                                              : *s.apb == Protocol::Apb5 ? "apb5"
                                                                                         : "apb4"
                         : s.ahb5                           ? "ahb5"
                         : s.ahb                            ? "ahb_v3"
                         : s.axi3                           ? "axi3"
                         : s.protocol == Protocol::Axi4Lite ? "axi4_lite"
                                                            : "axi4"};
  });
  Json definition;
  Json &contract() { return definition["contract"]; }
  bool uses(Protocol protocol) const {
    return any(p.targets, [&](const auto &t) { return t.protocol == protocol; });
  }
  // Sub-page windows and non-32-bit addresses extend the dedicated profiles;
  // configurations without them state the basic contract text.
  bool extended(Protocol protocol) const {
    return any(p.targets, [&](const auto &t) {
      return t.protocol == protocol && (any(t.mappings,
                                            [](const MappingPlan &m) {
                                              return m.window.base % 4096 != 0 ||
                                                     m.window.size % 4096 != 0;
                                            }) ||
                                        t.local_bits != 32 || p.address_bits != 32);
    });
  }
  // The numbering behind the target IDs of the network (docs/transactions.md#target-identifiers).
  void target_id_record() {
    if (!any(p.targets, [](const auto &t) { return t.id_bits.value_or(0) != 0; }))
      return;
    const auto &ids = p.route_index.target_ids;
    definition["target_ids"] = {{"route_bits",
                                 {{"source_path", ids.initiator_bits},
                                  {"target_path", ids.target_bits},
                                  {"region_index", 0},
                                  {"sequence", ids.sequence_bits},
                                  {"direction", ids.direction_bits}}},
                                {"route_fields", route_fields(ids)},
                                {"record_fields", route_fields(ids, true)},
                                {"source_paths", flow_indices(ids.initiators, ids.source_paths)},
                                {"target_paths", flow_indices(ids.targets, ids.target_paths)}};
    contract()["target_id"] = "flow_number_in_the_lowest_bits_one_source_flow_AxID_and_direction_"
                              "keep_one_target_ID_requests_may_share_a_target_ID_any_change_of_"
                              "names_endpoints_mappings_connectivity_or_source_ID_widths_may_"
                              "change_all_of_them";
  }
  void network_record() {
    definition = {{"name", p.name},
                  {"address_width", p.address_bits},
                  {"domain", p.domain.name},
                  {"initiators", names(p.sources)},
                  {"targets", Json::array()},
                  {"transport",
                   {{"data_width", p.burst_data_bits},
                    {"id_bits", h.plan.burst_id_bits},
                    {"len_bits", h.plan.burst_len_bits},
                    {"max_outstanding", p.max_outstanding},
                    {"transport_word_bytes", p.transport_word_bytes}}}};
    contract() = {
        {"profile", "axi4_burst_exchange_v1"},
        {"capacity", "source_bursts_in_flight_shared_by_reads_and_writes"},
        {"reset", "common_cancel_all_initiators_network_targets"},
        {"routing", !topology.contains("generated")
                        ? "explicit_independent_unique_route_graphs"
                        : "generated_one_switch_per_direction_connected_inputs_only"},
        {"arbitration", arbitration_contract(h.plan)},
        {"progress", "shared_read_write_routes_need_endpoint_premises_E1_E2_E3"},
        {"address_transform", "target_local_byte_offset_once"},
        {"start_address", "raw_AxADDR_page_offset_beside_aligned_occupancy"},
        {"transport_address",
         {{"address_bits", p.transport_address_bits},
          {"target_bits", p.target_bits},
          {"routing", "requests_by_target_index_decoded_at_source_target_restores_span"}}},
        {"attributes", "AxLEN_AxSIZE_AxBURST_AxPROT_AxID_preserved_AxCACHE_amba_mapping"},
        {"omitted_attributes", "AxQOS_AxREGION_absent_at_every_port"},
        {"exclusive", "AxLOCK_ignored_normal_access_OKAY_never_EXOKAY_AXI_A7_2_5"},
        {"burst_admission",
         "size_within_bus_INCR_within_4KiB_FIXED_at_most_16_WRAP_aligned_2_4_8_16"},
        {"unmapped", "DECERR_no_target_access_read_data_zero"},
        {"target_response", "OKAY_SLVERR_DECERR_preserved_EXOKAY_rejected"},
        {"read_error", "zero_error_beat_data_continue_remaining_beats"},
        {"write_error", "complete_all_beats_no_rollback"},
        {"write_data_unstrobed_bytes", "zero_at_every_target_cleared_at_source_capture"},
        {"write_last_mismatch", "descriptor_driven_target_length_source_SLVERR"}};
    if (p.id_table_entries)
      definition["transport"]["id_table_entries"] = *p.id_table_entries;
    definition["reset_ports"] = Json::array();
    for (const auto &port : p.reset_ports) {
      Json record = {{"name", port.name},
                     {"release", std::array{"resynchronized", "all_clocks_off",
                                            "on_clock_edge"}[static_cast<int>(port.release)]}};
      if (port.release_domain)
        record["release_domain"] = *port.release_domain;
      definition["reset_ports"].push_back(std::move(record));
    }
    definition["clock_port"] = p.domain.clock_port;
    definition["reset_port"] = p.domain.reset_port;
    domain_gating(definition, p.domain);
    for (const auto &port : p.test_ports)
      definition["test_ports"].push_back({{"name", port.name}});
    for (const auto &port : p.enable_ports)
      definition["enable_ports"].push_back({{"name", port.name}});
    for (const auto &port : p.ungate_ports)
      definition["ungate_ports"].push_back({{"name", port.name}});
    for (const auto &port : p.address_inputs)
      definition["address_inputs"].push_back(
          {{"name", port.name}, {"width", port.width}, {"clock_domain", port.clock_domain}});
    for (const auto &flag : p.address_selectors)
      definition["address_selectors"].push_back({{"name", flag.name}, {"equals", flag.equals}});
    if (p.clock_gating != ClockGating::None)
      definition["clock_gating"] = "per_switch_and_endpoint";
    if (h.ir.synchronous_reset)
      definition["register_reset"] = "synchronous";
  }
  void endpoint_transactions() {
    Json endpoints = Json::object(), targets = Json::object();
    for (const auto &s : p.sources) {
      Json entry = Json::object();
      if (s.id_bits)
        entry = {{"id_width", *s.id_bits}, {"len_width", *s.len_bits}};
      if (s.read_only || s.write_only)
        entry["access"] = s.read_only ? "read_only" : "write_only";
      if (!s.flows.empty()) {
        entry["route_classes"] = s.flows;
        entry["default_class"] = s.default_class;
        entry["id_ranges"] = Json::array();
        for (const auto &range : s.id_ranges)
          entry["id_ranges"].push_back(
              {{"first", range.first}, {"last", range.last}, {"class", s.flows[range.path]}});
      }
      if (!entry.empty())
        endpoints[s.name] = entry;
    }
    for (const auto &t : p.targets) {
      if (t.id_bits)
        targets[t.name] = {{"id_width", *t.id_bits}, {"len_width", *t.len_bits}};
      if (t.flows > 1)
        targets[t.name]["flows"] = t.flows;
    }
    Json transactions = Json::object();
    if (!endpoints.empty())
      transactions["initiators"] = endpoints;
    if (!targets.empty())
      transactions["targets"] = targets;
    if (any(p.sources, [](const auto &s) { return s.id_bits.has_value(); }) || !targets.empty()) {
      transactions["id"] = "source_port_zero_extended_into_the_network_id_target_port_carries_"
                           "the_target_id_of_contract_target_id";
      transactions["len"] = "source_port_limits_its_bursts_target_port_limits_its_packets";
    }
    if (any(p.targets, [](const TargetPlan &t) { return t.flows > 1; }))
      transactions["target_paths"] =
          "apb_one_port_set_per_flow_axi_flow_number_in_the_lowest_id_bits_by_the_mapping_holding_"
          "the_burst";
    if (p.burst_path_bits)
      transactions["route_path"] =
          "request_source_path_response_return_path_bits_" + std::to_string(p.burst_path_bits);
    if (any(p.sources, &SourcePlan::read_only))
      transactions["read_only_source"] = "no_AW_W_B_ports";
    if (any(p.targets, &TargetPlan::read_only)) {
      transactions["read_only_targets"] = names_if(p.targets, &TargetPlan::read_only);
      transactions["read_only_target"] =
          "write_absorbs_all_W_then_one_SLVERR_B_no_target_access_AXI4_no_AW_W_B_ports";
    }
    if (any(p.sources, &SourcePlan::write_only))
      transactions["write_only_source"] = "no_AR_R_ports";
    if (any(p.targets, &TargetPlan::write_only)) {
      transactions["write_only_targets"] = names_if(p.targets, &TargetPlan::write_only);
      transactions["write_only_target"] =
          "read_answers_SLVERR_zero_data_every_beat_no_target_access_AXI4_no_AR_R_ports";
    }
    if (!transactions.empty())
      definition["endpoint_transactions"] = transactions;
  }
  void target_widths() {
    if (!apb_widths.empty())
      definition["apb_target_data_widths"] = apb_widths;
    if (!p.per_endpoint)
      return;
    // Only targets off the network width are listed; wide sources alone list none.
    if (auto widths = by_name(p.targets,
                              [&](const auto &t) {
                                return t.data_bits != p.burst_data_bits ? std::optional{t.data_bits}
                                                                        : std::nullopt;
                              });
        !widths.empty())
      definition["target_data_widths"] = std::move(widths);
    auto &widths = contract()["endpoint_widths"];
    widths = {
        {"attachment", "each_endpoint_at_its_own_width_adapters_place_beats_by_address"},
        {"wide_to_narrow", "burst_clamped_to_the_width_of_the_target_at_its_start_address"},
        {"clamp_incr", "target_words_from_the_start_word_on_in_INCR_pieces_of_at_most_2_pow_len_"
                       "bits_beats"},
        {"clamp_wrap", "same_container_one_WRAP_up_to_16_target_beats_else_INCR_pieces_start_to_"
                       "top_then_base_to_start"},
        {"clamp_fixed", "wider_than_target_never_cut_SLVERR_no_target_access_window_decode_first"},
        {"clamp_pieces", "up_to_four_pieces_in_flight_one_merged_B_network_miss_first"},
        {"clamp_reject", "FIXED_wider_than_target_or_exclusive_burst_needing_more_than_one_"
                         "piece_SLVERR_no_target_access_window_decode_first"},
        {"clamp_read", "target_words_gathered_into_source_beats_errors_merged"},
        {"axi4_lite_source", "single_source_beat_clamped_to_target_words_one_B_or_gathered_R"}};
    // Per-target widths: AXI4 targets merge narrow bursts (a wide source alone keeps them).
    if (p.per_target) {
      widths["narrow_to_wide"] =
          "AXI4_target_merges_bursts_into_target_words_whatever_their_AxCACHE";
      widths["narrow_bursts_default"] = "merge";
    }
    if (multi_source)
      widths["clamp_outstanding"] = serial_pieces;
    // Describe attachment widths above 256 bits.
    auto own = [&](const auto &t) { return t.data_bits != p.burst_data_bits ? t.data_bits : 0U; };
    const auto widest =
        std::max(std::ranges::max(p.targets | std::views::transform(own)),
                 std::ranges::max(p.sources, {}, &SourcePlan::engine_bits).engine_bits);
    if (widest > 256)
      widths["wide_attachment"] =
          "widest_endpoint_" + std::to_string(widest) +
          "_bits_steps_down_by_2to1_transport_adapters_to_each_switch_word_AxSIZE_at_most_" +
          std::to_string(std::bit_width(widest / 8) - 1) + "_2048_bits_rejected";
  }
  void target_attributes() {
    if (!any(p.targets, [](const auto &t) { return t.attributes.has_value(); }))
      return;
    Json targets = Json::object();
    for (const auto &t : p.targets) {
      if (!t.attributes)
        continue;
      Json entry = Json::object();
      if (t.protocol == Protocol::Axi4) {
        entry["read_cache"] = attribute_bits(t.attributes->read_cache);
        entry["write_cache"] = attribute_bits(t.attributes->write_cache);
      }
      if (t.protocol != Protocol::AhbV3)
        entry["prot"] = attribute_bits(t.attributes->prot);
      else
        entry["hprot"] = attribute_bits(t.attributes->hprot);
      targets[t.name] = entry;
    }
    definition["target_attributes"] = targets;
    contract()["target_attributes"] =
        "listed_targets_map_each_attribute_bit_to_0_1_or_a_source_flag_bit0_first_allocate_is_"
        "AxCACHE2_on_reads_AxCACHE3_on_writes_lookup_is_either_AHB_HPROT0_inverted_AXI4_AxLOCK_0";
  }
  // Network user flags: each source's driver of every flag and each target's AxUSER bits.
  // The response user field and the BUSER and RUSER widths of the endpoints that have them.
  void response_user() {
    Json record = Json::object();
    for (const auto &s : p.sources)
      if (s.response_user_bits)
        record["initiators"][s.name] = s.response_user_bits;
    for (const auto &t : p.targets)
      if (t.response_user_bits)
        record["targets"][t.name] = t.response_user_bits;
    if (!p.burst_response_user_bits && record.empty())
      return;
    record["bits"] = p.burst_response_user_bits;
    definition["response_user"] = record;
    contract()["response_user"] =
        "a_response_packet_carries_the_target_BUSER_or_the_RUSER_of_its_first_beat_a_source_takes_"
        "the_low_bits_of_its_final_packet_B_zero_above_and_on_local_answers";
  }
  void data_user() {
    if (!p.data_user_bits_per_byte)
      return;
    Json record = {{"bits_per_byte", p.data_user_bits_per_byte}};
    for (const auto &s : p.sources)
      if (s.data_user_bits)
        record["initiators"][s.name] = s.data_user_bits;
    for (const auto &t : p.targets)
      if (t.data_user_bits)
        record["targets"][t.name] = t.data_user_bits;
    definition["data_user"] = record;
    contract()["data_user"] =
        "each_byte_keeps_its_user_group_through_width_conversion_strobed_off_write_bytes_"
        "have_zero_user_groups_a_failed_source_read_returns_zero_in_every_PRUSER_bit";
  }
  void user_flags() {
    response_user();
    data_user();
    if (p.user_flags.empty())
      return;
    const auto text = [&](const AttributeBit &bit, bool target) {
      const auto source = bit.source != AttributeSource::User
                              ? attribute_bits(std::array{bit}).front().get<std::string>()
                          : target ? p.user_flags.at(bit.index)
                                   : "user[" + std::to_string(bit.index) + "]";
      return source.starts_with('~') == bit.invert ? source : "~" + source;
    };
    Json record = {{"flags", p.user_flags}};
    for (const auto &s : p.sources) {
      Json entry = {{"user_width", s.user_bits}};
      for (std::size_t i = 0; i < s.user_flags.size(); ++i)
        entry["drivers"][p.user_flags[i]] = text(s.user_flags[i], false);
      record["initiators"][s.name] = entry;
    }
    for (const auto &t : p.targets)
      if (!t.user.empty())
        for (const auto &bit : t.user)
          record["targets"][t.name].push_back(text(bit, true));
    definition["user_flags"] = record;
    contract()["user_flags"] =
        "every_request_packet_carries_the_flags_its_source_drove_each_target_AxUSER_bit_is_0_1_"
        "or_a_flag_bit0_first";
  }
  void packets() {
    if (!any(p.sources, [](const auto &s) { return s.protocol == Protocol::Axi4; }))
      return;
    Json limits = Json::array();
    for (const auto &pair : p.pairs) {
      if (pair.limit_field.empty())
        continue;
      Json entry = {{"source", p.sources[pair.source].name},
                    {"target", p.targets[pair.target].name}};
      if (pair.max_bytes)
        entry["max_bytes"] = pair.max_bytes;
      if (pair.boundary_bytes)
        entry["boundary_bytes"] = pair.boundary_bytes;
      limits.push_back(entry);
    }
    if (!limits.empty())
      definition["transport"]["packet_limits"] = limits;
    const auto split = names_if(p.sources, &SourcePlan::split_narrow);
    if (!split.empty())
      definition["transport"]["decompose_exact_narrow_beats"] = split;
    auto &packets = contract()["packets"];
    packets = {
        {"cut", "INCR_from_the_start_word_max_source_target_bytes_per_packet_limit_and_"
                "packet_boundary_of_the_target_window_FIXED_unsplit_locked_SLVERR_if_cut"},
        {"limit", "AXI4_min_2_pow_AxLEN_words_4KiB_APB_and_Lite_128B_AHB_16_words_a_pair_limit_"
                  "may_narrow_limit_and_boundary_down_to_the_wider_data_width"},
        {"wrap", "cut_by_source_and_target_WRAP_limits_exclusive_WRAP_sent_as_INCR"},
        {"boundary", "AXI4_min_4KiB_local_space_AHB_min_1KiB_local_space_APB_and_AXI4_Lite_"
                     "local_space_then_the_largest_power_of_two_dividing_window_base_and_size"},
        {"wide_to_32", "converter_rejects_an_unaligned_wide_INCR_in_its_first_packet_only"},
        {"narrow", "decompose_exact_narrow_beats_sources_send_each_beat_of_a_narrow_INCR_with_"
                   "AxCACHE1_clear_as_a_packet_each_piece_one_unit_of_the_source_pool"},
        {"issue", "up_to_four_packets_of_one_burst_in_flight_per_direction"},
        {"pool",
         "reads_and_writes_share_max_outstanding_transactions_in_flight_the_last_"
         "unanswered_response_of_a_burst_frees_its_unit_narrow_split_pieces_count_one_each"},
        {"response", "write_one_B_any_packet_error_merged_network_miss_first_read_first_last_"
                     "span_the_burst"}};
    if (any(p.targets, &TargetPlan::read_side_effects)) {
      definition["read_side_effects_targets"] = names_if(p.targets, &TargetPlan::read_side_effects);
      packets["exact"] =
          "INCR_with_AxCACHE1_and_AxLOCK_clear_starting_in_such_a_target_goes_out_as_naturally_"
          "aligned_power_of_two_blocks_one_access_each_reads_gathered_into_source_beats";
    }
    if (any(p.sources,
            [](const auto &s) { return s.outstanding > 1 && s.protocol == Protocol::Axi4; })) {
      packets["issue"] = "up_to_four_pieces_of_serial_bursts_in_flight_per_direction_narrow_split_"
                         "pieces_take_pool_units_instead";
      packets["outstanding"] =
          "single_packet_passthrough_preserves_tag_serial_write_or_read_takes_a_vacant_reassembly_"
          "context_of_its_own_a_write_also_waits_for_packet_room_never_for_older_bursts";
    }
    if (!split.empty())
      packets["narrow_exclusive"] =
          "decompose_exact_narrow_beats_sources_never_send_a_narrow_INCR_with_AxLOCK_set_one_beat_"
          "per_packet_packet_limits_still_cut_it_at_targets_without_exclusive_access_handling";
  }
  void error_responses() {
    definition["transport"]["response_codes"] = p.error_codes ? "distinct" : "uniform";
    if (p.error_codes) {
      contract()["error_codes"] = {
          {"encoding", "decode_and_connectivity_misses_and_AXI4_AXI4_Lite_target_DECERR_DECERR_"
                       "other_failures_SLVERR"},
          {"read_packet",
           "each_source_beat_merges_only_its_target_words_DECERR_before_SLVERR_before_OKAY"},
          {"axi4_lite_write", "first_failed_word_code"},
          {"wide_read_beat", "any_failed_word_zeroes_the_whole_beat"}};
    } else {
      contract()["error_responses"] = {
          {"encoding", "every_failure_SLVERR_including_decode_and_admission_misses"},
          {"wide_read_beat", "any_failed_word_zeroes_the_whole_beat"}};
    }
  }
  // Topology, admitted pairs, windows in address order, endpoint address widths.
  void routing() {
    definition["topology"] = topology;
    definition["connectivity"] = Json::array();
    // When the matrix leaves out flow pairs, every pair lists the [initiator flow, target flow]
    // pairs it connects.
    const auto &unconnected = p.exchange.request.graph.unconnected;
    for (const auto &pair : *p.exchange.request.graph.admitted_pairs) {
      Json row{{"source", pair.entry}, {"target", pair.exit}};
      const auto &source = *std::ranges::find(p.sources, pair.entry, &SourcePlan::name);
      if (!unconnected.empty()) {
        row["flow_pairs"] = Json::array();
        for (unsigned f = 0; f < std::max<std::size_t>(1, source.flows.size()); ++f)
          for (unsigned g = 0; g < p.target(pair.exit).flows; ++g)
            if (!unconnected.contains({pair.entry, pair.exit, f, g}))
              row["flow_pairs"].push_back({f, g});
      }
      definition["connectivity"].push_back(std::move(row));
    }
    contract()["connectivity"] = {
        {"unconnected_pair_or_address_outside_routing_windows",
         "DECERR_from_the_reached_target_with_the_smallest_window_by_error_request_without_"
         "target_handshake"},
        {"routing_window", "target_window_or_whole_4KiB_page_of_a_sub_page_window"},
        {"sub_page_remainder", "enters_the_network_outlet_answers_DECERR_without_target_handshake"},
        {"protocol_violation_priority", "SLVERR_before_route_decode"},
        {"capacity", "a_miss_takes_one_of_the_source_bursts_in_flight"},
        {"switch_decode", "each_switch_input_compares_only_the_windows_or_sources_it_can_carry"}};
    for (const auto &s : p.sources)
      if (s.default_target) {
        definition["default_targets"][s.name] = *s.default_target;
        contract()["connectivity"]["named_default_target"] = "the_listed_sources_send_misses_to_it";
      }
    if (any(p.targets, [](const auto &t) {
          return any(t.mappings, [](const MappingPlan &m) {
            return m.window.size % 4096 || m.routes != std::vector{m.window};
          });
        })) {
      auto &c = contract()["connectivity"];
      c.erase("sub_page_remainder");
      c["routing_window"] = "target_window_less_the_smaller_mappings_overlapping_it";
      c["cut_page"] = "packets_cut_at_every_mapping_edge_one_outside_the_mappings_an_error_"
                      "request_to_the_default_target_DECERR_without_access";
    }
    // Every mapping in address order; local only when it differs from the offset.
    std::vector<std::pair<const TargetPlan *, const MappingPlan *>> mappings;
    for (const auto &t : p.targets)
      std::ranges::transform(t.mappings, std::back_inserter(mappings),
                             [&](const MappingPlan &m) { return std::pair{&t, &m}; });
    // Global intervals: a mode-dependent mapping at its own address with its condition.
    const auto global = [&](const MappingPlan &m) {
      return m.window.base - (std::uint64_t{m.plane} << p.plane_shift);
    };
    std::ranges::sort(mappings, {},
                      [&](const auto &x) { return std::pair{global(*x.second), x.second->plane}; });
    std::ranges::transform(mappings, std::back_inserter(definition["targets"]), [&](const auto &x) {
      const auto &[t, m] = x;
      Json row{{"name", t->name},
               {"begin", decimal(global(*m))},
               {"end", decimal(global(*m) + m->window.size)}};
      if (m->local_base)
        row["local"] = decimal(m->local_base);
      if (!m->when.empty())
        row["when"] = m->when;
      if (m->flow)
        row["flow"] = m->flow;
      return row;
    });
    if (!any(p.sources, [&](const auto &s) { return s.address_bits != p.address_bits; }) &&
        !any(p.targets, [&](const auto &t) { return t.local_bits != p.address_bits; }))
      return;
    definition["endpoint_address_widths"] = {
        {"initiators", field_by_name(p.sources, &SourcePlan::address_bits)},
        {"targets", field_by_name(p.targets, &TargetPlan::local_bits)},
        {"transport", p.burst_address_bits},
        {"target_address", "local_byte_offset"}};
  }
  // Per-endpoint attachment: every endpoint at its own width.
  void width_conversion() {
    if (!p.per_endpoint)
      return;
    definition["endpoint_data_widths"] = {
        {"initiators", field_by_name(p.sources, &SourcePlan::engine_bits)},
        {"targets", field_by_name(p.targets, &TargetPlan::data_bits)},
        {"transport", "per_endpoint_attachment"}};
    contract()["attributes"] =
        "ID_preserved_CACHE_PROT_follow_target_attributes_default_amba_ADDR_LEN_SIZE_BURST_"
        "follow_narrow_to_wide_clamp_and_packets";
  }
  void ingress() {
    const auto lite_face = [](const auto &s) { return s.protocol == Protocol::Axi4Lite && !s.apb; };
    if (any(p.sources,
            [](const auto &s) { return s.protocol == Protocol::Axi4Lite || s.ahb || s.axi3; }))
      definition["endpoint_protocols"] = {{"initiators", source_protocols}, {"targets", "axi4"}};
    if (any(p.sources, &SourcePlan::axi3))
      contract()["axi3_ingress"] = {
          {"bursts", "AXI4_bursts_of_at_most_16_beats"},
          {"lock", "AxLOCK_01_exclusive_00_normal_10_locked_and_11_reserved_answer_SLVERR_"
                   "without_target_access"},
          {"write_data", "W_beats_follow_AW_order_no_write_interleaving_a_beat_whose_WID_differs_"
                         "from_its_AWID_is_written_and_the_burst_answers_SLVERR_as_for_WLAST"}};
    if (any(p.sources, &SourcePlan::ahb))
      contract()["ahb_ingress"] = {
          {"transfer", "AHB_Lite_subordinate_one_burst_in_flight_HREADYOUT_low_until_the_beat_"
                       "moves_IDLE_and_BUSY_zero_wait_OKAY"},
          {"bursts", "SINGLE_one_beat_of_HSIZE_INCR4_8_16_and_WRAP4_8_16_one_AXI_burst_"
                     "undefined_INCR_one_piece_per_committed_transfer_without_read_ahead"},
          {"early_end", "a_piece_ended_by_anything_but_SEQ_or_BUSY_sends_its_remaining_W_beats_"
                        "unstrobed_or_drops_its_remaining_R_beats"},
          {"writes", "bufferable_HPROT2_beats_complete_on_W_acceptance_and_ignore_the_B_other_"
                     "writes_complete_their_last_beat_with_the_B"},
          {"attributes", "HPROT3_lookup_ARCACHE3_AWCACHE2_HPROT3_2_AxCACHE1_0_HPROT1_AxPROT0_"
                         "inverted_HPROT0_AxPROT2_AxPROT1_nonsecure_AxID_and_AxLOCK_zero"},
          {"error", "two_cycle_ERROR_for_a_failed_beat_including_unmapped_HRDATA_only_valid_on_"
                    "OKAY_reads"},
          {"unsupported", "HMASTLOCK_HSIZE_above_the_bus_and_narrow_HSIZE_outside_SINGLE_answer_"
                          "ERROR_without_target_access"},
          {"order", "every_request_issues_after_the_responses_of_earlier_writes"},
          {"reset", "cancel_the_burst_no_replay"}};
    if (p.ahb5) {
      definition["ahb5_properties"] = {
          {"initiators",
           by_name(p.sources,
                   [](const SourcePlan &s) {
                     return s.ahb5 ? std::optional{Json{{"hmaster_width", s.hmaster_bits},
                                                        {"hprot_width", s.hprot_bits},
                                                        {"hnonsec", s.hnonsec}}}
                                   : std::nullopt;
                   })},
          {"targets", by_name(p.targets, [](const TargetPlan &t) {
             return t.ahb5 ? std::optional{Json{{"hmaster_width", t.hmaster_bits},
                                                {"hprot_width", t.hprot_bits},
                                                {"hnonsec", t.hnonsec}}}
                           : std::nullopt;
           })}};
      contract()["ahb5_properties"] =
          "optional_HNONSEC_as_AxPROT1_HPROT_and_HMASTER_preserved_between_AHB5_endpoints_"
          "other_sources_HMASTER_zero_AXI4_shareable_zero_see_docs_ahb";
      if (any(p.sources, &SourcePlan::ahb5)) {
        contract()["ahb_ingress"]["writes"] =
            "bufferable_HPROT2_beats_complete_on_W_acceptance_and_ignore_the_B_unless_"
            "seven_bit_HPROT6_is_shareable_other_writes_complete_their_last_beat_with_the_B_"
            "and_preserve_errors";
        contract()["ahb_ingress"]["attributes"] =
            "AHB5_optional_HNONSEC_as_AxPROT1_HPROT_memory_types_as_AxCACHE_"
            "raw_HPROT_and_HMASTER_in_request_metadata_AxID_and_AxLOCK_zero";
      }
    }
    if (any(p.sources, lite_face))
      contract()["lite_ingress"] = {
          {"channels", "independent_AW_W_single_slot_each_independent_B_R"},
          {"mapping", "LEN0_SIZE_bus_bytes_INCR_ID0_LOCK0_CACHE0_PROT_WSTRB_preserved"},
          {"unaligned",
           "address_cleared_to_bus_width_before_checks_WSTRB_as_given_read_whole_word"},
          {"reset", "cancel_partial_AW_or_W_no_replay"}};
    // Listed only when an AXI4-Lite initiator keeps more than one transaction per direction
    // (APB initiators keep one).
    if (any(p.sources, [&](const auto &s) { return s.outstanding > 1 && lite_face(s); }))
      contract()["lite_ingress"]["outstanding"] =
          "one_ordering_key_per_direction_same_destination_pipelines_another_destination_waits_"
          "until_every_earlier_transaction_completes_responses_in_issue_order";
    if (any(p.sources, [&](const auto &s) { return lite_face(s) && s.id_table < s.id_keys; }))
      contract()["lite_ingress"]["id_table"] =
          "one_shared_entry_for_the_read_and_write_ordering_keys_the_other_direction_waits_"
          "until_the_entry_frees";
    if (any(p.sources, [](const auto &s) { return s.apb.has_value(); }))
      contract()["apb_ingress"] = {
          {"transfer", "one_transfer_issued_from_SETUP_PREADY_registered_after_the_response"},
          {"mapping", "single_32bit_INCR_beat_ID0_LOCK0_CACHE0_PPROT_as_AxPROT_PSTRB_as_WSTRB"},
          {"apb3", "PSTRB_all_ones_PPROT_unprivileged_nonsecure_data"},
          {"unaligned", "PADDR_cleared_to_the_word_before_checks_PSTRB_as_given_read_whole_word"},
          {"error", "PSLVERR_for_every_failure_including_unmapped_PRDATA_zero"},
          {"reset", "cancel_the_transfer_no_replay"}};
    if (!any(p.sources, [](const auto &s) { return s.apb_bits != 32; }))
      return;
    auto &widths = definition["endpoint_data_widths"];
    widths["initiators"] = by_name(p.sources, [](const auto &s) {
      return std::optional{s.apb_bits != 32 ? s.apb_bits : s.engine_bits};
    });
    if (!widths.contains("targets")) {
      widths["targets"] = p.burst_data_bits;
      widths["transport"] = p.burst_data_bits;
    }
    auto &apb = contract()["apb_ingress"];
    apb["data_widths"] = by_name(p.sources, [](const auto &s) {
      return s.apb_bits != 32 ? std::optional{s.apb_bits} : std::nullopt;
    });
    apb["mapping"] =
        "single_source_word_INCR_beat_in_32bit_engine_ID0_LOCK0_CACHE0_PPROT_as_AxPROT";
    apb["lanes"] =
        "source_word_address_selects_32bit_byte_lanes_PWDATA_PSTRB_shifted_PRDATA_extracted";
  }
  void target_contracts() {
    const bool has_ahb = uses(Protocol::AhbV3),
               has_apb = uses(Protocol::Apb4) || uses(Protocol::Apb5);
    const bool has_axi3 = any(p.targets, &TargetPlan::axi3);
    const bool has_dialects =
        uses(Protocol::Apb3) || uses(Protocol::Apb2) || uses(Protocol::Axi4Lite) || has_axi3;
    if (!has_ahb && !has_apb && !has_dialects)
      return;
    if (!definition.contains("endpoint_protocols"))
      definition["endpoint_protocols"]["initiators"] = source_protocols;
    static const char *const protocols[] = {"apb4",   "axi4_lite", "apb3", "apb2", "axi4",
                                            "ahb_v3", "axi3",      "ahb5", "apb5"};
    // An AXI3 target travels as an AXI4 target; its own protocol names it.
    definition["endpoint_protocols"]["targets"] = by_name(p.targets, [](const TargetPlan &t) {
      return std::optional{t.service ? "service"
                           : t.axi3  ? "axi3"
                           : t.ahb5  ? "ahb5"
                                     : protocols[static_cast<int>(t.protocol)]};
    });
    if (has_axi3)
      contract()["axi3_egress"] = {
          {"bursts", "AXI4_bursts_of_at_most_16_target_words_len_width_4"},
          {"lock", "AxLOCK_01_exclusive_with_exclusive_target_else_00_never_locked"},
          {"write_data", "W_beats_in_AW_order_WID_is_the_AWID_of_the_beat_burst_no_write_"
                         "interleaving"}};
    if (has_ahb)
      ahb_targets();
    if (has_apb)
      apb_targets(has_ahb);
    service_records();
    if (any(p.targets, &TargetPlan::back_to_back)) {
      definition["apb_back_to_back_targets"] = names_if(p.targets, &TargetPlan::back_to_back);
      contract()["apb_back_to_back"] = {
          {"access", "SETUP_then_ACCESS_without_idle_when_ready"},
          {"read_response_capacity", 2},
          {"packet_capacity_per_direction", 1},
          {"reservation", "reserve_response_space_before_read_SETUP"}};
    }
    if (!apb_widths.empty()) {
      contract()["apb_target_words"] = {
          {"physical_data_widths", apb_widths},
          {"container", "32bit_internal_word_physical_8_16_32bit_APB_word"},
          {"address", "local_offset_aligned_to_physical_word_lowest_enabled_word_first"},
          {"write",
           "physical_subwords_from_container_byte_lanes_zero_strobe_quiet_"
           "APB3_APB2_partial_subword_no_access_later_subwords_continue_packet_error_accumulates"},
          {"read", "gather_requested_lanes_one_response_per_container_error_zero_all_data_"
                   "later_subwords_continue"},
          {"exclusive", "APB3_APB2_exclusive_beats_still_require_at_least_32bits"}};
      if (has_apb)
        contract()["apb_targets"]["profile"] =
            "dedicated_APB4_8_16_32bit_physical_data_32bit_container";
    }
    if (has_dialects)
      dedicated_dialects();
    if (any(p.targets, &TargetPlan::fixed_single))
      fixed_bursts();
  }
  void ahb_targets() {
    auto &ahb = contract()["ahb_targets"];
    ahb = {
        {"profile", "dedicated_32bit_no_shared_bus_retry"},
        {"transfer", "one_burst_per_packet_INCR4_8_16_WRAP4_8_16_SINGLE_or_INCR_NONSEQ_then_SEQ_"
                     "pipelined_address_phase_overlaps_data_phase_one_transfer_per_HREADY_"
                     "cycle_unbounded_BUSY_while_next_word_or_response_room_waits_"
                     "reads_complete_fixed_length_bursts_writes_INCR_or_SINGLE_"
                     "partial_strobes_NONSEQ_SINGLE_subwords"},
        {"progress",
         "AXI_sources_supply_all_W_and_accept_all_R_without_dependency_on_other_transactions_"
         "cross_source_dependencies_not_resolved"},
        {"read", "one_beat_or_FIXED_exact_suffix_in_aligned_subreads_gather_one_response_"
                 "longer_reads_AxSIZE_container_error_data_zero_continue"},
        {"write", "container_start_block_or_word_half_one_transfer_else_bytes_zero_strobe_quiet"},
        {"response", "ERROR_target_error_RETRY_SPLIT_abort_packet_target_error_1bit_ERROR_"
                     "2bit_AMBA2_widths_in_ahb_hresp_widths"},
        {"fixed", fixed_reject},
        {"protection", "HPROT3_2_CACHE1_0_HPROT1_PROT0_HPROT0_not_PROT2"},
        {"capacity", "three_write_packet_contexts_two_write_words_independent_B_snapshot_"
                     "one_read_issue_descriptor_two_response_metadata_slots_"
                     "counting_read_transfers_in_flight_shared_pipelined_bus"}};
    if (extended(Protocol::AhbV3))
      ahb["address"] = address_contract();
    // Wider AHB words (their widths are in target_data_widths); 32-bit
    // configurations keep the text above.
    if (any(p.targets, [&](const auto &t) {
          return t.data_bits != p.burst_data_bits && t.data_bits > 32 &&
                 t.protocol == Protocol::AhbV3;
        })) {
      ahb["profile"] = "dedicated_no_shared_bus_retry";
      ahb["data_width"] =
          "32_to_1024_bits_per_target_beat_keeps_AxSIZE_as_HSIZE_HADDR_steps_by_2_pow_AxSIZE_"
          "packets_of_beats_of_32_bits_or_more_are_bursts_byte_and_halfword_packets_SINGLE_"
          "write_word_full_when_strobes_cover_its_AxSIZE_container_partial_word_one_transfer_"
          "for_its_start_block_or_a_target_word_half_else_bytes_WRAP16_of_128_byte_beats_SINGLE_"
          "narrower_sources_merged_into_target_words";
    }
    // HRESP port width of every AHB target: 1 (AHB-Lite/AHB5) or 2 (AMBA 2).
    definition["ahb_hresp_widths"] = by_name(p.targets, [](const auto &t) {
      return t.protocol == Protocol::AhbV3 ? std::optional{t.hresp_bits} : std::nullopt;
    });
    // Listed only with a 2-bit target; 1-bit outlets keep the text above.
    if (any(p.targets, [](const auto &t) { return t.hresp_bits == 2; }))
      ahb["amba2_hresp"] =
          "RETRY_SPLIT_two_cycle_cancel_next_address_abort_current_packet_target_error_"
          "unrelated_packet_restarts_NONSEQ_ERROR_continues_burst_no_HMASTER_HSPLIT";
    contract()["attributes"] = "per_target_protocol_mapping_see_ahb_targets";
    contract()["target_response"] = "AXI_response_preserved_AHB_ERROR_to_SLVERR";
  }
  // What a map says of service targets and error loggers; absent without them.
  void service_records() {
    for (const auto &t : p.targets) {
      if (!t.service)
        continue;
      const auto record = [](const ServiceBank &bank) -> Json {
        auto fields = bank.plan.fields;
        std::ranges::sort(fields, {},
                          [](const RegisterField &f) { return std::pair{f.word, f.lsb}; });
        Json registers = Json::array();
        std::ranges::transform(fields, std::back_inserter(registers),
                               [](const RegisterField &f) -> Json {
                                 Json entry = {{"name", f.name},
                                               {"offset", 4 * f.word},
                                               {"lsb", f.lsb},
                                               {"width", f.width},
                                               {"access", register_access_name(f.access)}};
                                 if (f.access == RegisterAccess::Constant)
                                   entry["value"] = f.value;
                                 if (f.access == RegisterAccess::Control)
                                   entry["reset_value"] = f.value;
                                 if (!f.unit.empty())
                                   entry["unit"] = f.unit;
                                 if (!f.description.empty())
                                   entry["description"] = f.description;
                                 return entry;
                               });
        return {{"bank", bank.plan.name},
                {"route", bank.plan.route},
                {"registers", std::move(registers)}};
      };
      const auto &banks = t.service->banks;
      auto entry = record(banks.front());
      entry["base"] = banks.front().base;
      entry["bank_bytes"] = std::uint64_t{1} << banks.front().plan.address_bits;
      entry["access_kinds"] = {
          {"constant", "Reads its value and ignores writes"},
          {"control", "Reads back. Written under its byte's strobe. reset_value after reset"},
          {"status", "Reads a hardware value and ignores writes"},
          {"pulse", "Reads zero. A written one under its byte's strobe acts for one cycle"}};
      entry["unmapped"] = "An address of a bank that no register names reads zero and ignores "
                          "writes without an error";
      entry["error_codes"] = {{"none", 0}, {"target", CodeTarget}, {"decode", CodeDecode}};
      if (banks.size() > 1) {
        entry["banks"] = Json::array();
        for (const auto &bank : banks) {
          auto placed = record(bank);
          placed["base"] = bank.base;
          entry["banks"].push_back(std::move(placed));
        }
      }
      definition["log_registers"][t.name] = std::move(entry);
    }
    for (const auto &o : p.fault_monitors) {
      Json observed = Json::array();
      std::ranges::transform(
          o.observed, std::back_inserter(observed),
          [&](const ObservedTarget &seen) { return p.targets[seen.target].name; });
      definition["error_logs"][o.logger.name] = {
          {"registers", p.targets[o.service].name},
          {"watch", std::move(observed)},
          {"serial_words", o.logger.serial_words},
          {"alarm_port", "nl_o_" + o.logger.name + "_alarm"}};
      const auto layout = error_record_layout(o.logger);
      auto &log = definition["error_logs"][o.logger.name];
      log["record_bits"] = layout.bits;
      log["serial_word_bits"] = o.logger.serial_word_bits;
      log["transfer_words"] = o.logger.crossed ? o.logger.serial_words : 1;
      log["record_fields"] = Json::array();
      for (const auto &[name, width, lsb] : layout.fields)
        log["record_fields"].push_back({{"name", name}, {"width", width}, {"lsb", lsb}});
      const auto &ids = p.route_index.target_ids;
      log["route_fields"] = route_fields(ids, true);
      definition["error_logs"][o.logger.name]["route_values"] = {
          {"source_path", flow_indices(ids.initiators, ids.source_paths)},
          {"target_path", flow_indices(ids.targets, ids.target_paths)}};
      if (!p.security_flags.empty())
        definition["error_logs"][o.logger.name]["audit_flags"] = p.security_flags;
      if (!o.logger.chain.empty()) {
        Json ports = Json::array();
        for (std::size_t n = 0; n < o.logger.chain.size(); ++n)
          ports.push_back("nl_o_" + o.logger.name + "_alarm" + std::to_string(n + 1));
        definition["error_logs"][o.logger.name]["chain_alarm_ports"] = std::move(ports);
      }
    }
  }
  void apb_targets(bool has_ahb) {
    auto &apb = contract()["apb_targets"];
    apb = {{"profile", "dedicated_APB4_32bit_data_and_address_4KiB_windows"},
           {"transfer", "one_SETUP_cycle_then_ACCESS_until_PREADY"},
           {"read", lane_read},
           {"write", "PSTRB_preserved_zero_strobe_quiet_no_rollback"},
           {"response", "PSLVERR_at_completion_to_SLVERR_later_beats_continue"},
           {"fixed", fixed_reject},
           {"protection", "PPROT_from_AxPROT_CACHE_not_represented"},
           {"reset", "common_reset_cancels_SETUP_ACCESS_and_pending_responses"},
           {"capacity", direction_capacity}};
    if (uses(Protocol::Apb5))
      apb["profile"] = "dedicated_APB_32bit_engine_PAUSER_PBUSER";
    if (extended(Protocol::Apb4) || extended(Protocol::Apb5)) {
      apb["profile"] = uses(Protocol::Apb5) ? "dedicated_APB_32bit_engine_PAUSER_PBUSER"
                                            : "dedicated_APB4_32bit_data";
      apb["address"] = address_contract();
    }
    contract()["attributes"] = has_ahb
                                   ? "per_target_protocol_mapping_see_ahb_targets_and_apb_targets"
                                   : "per_target_protocol_mapping_see_apb_targets";
    contract()["target_response"] =
        has_ahb ? "AXI_response_preserved_AHB_nonzero_and_APB_PSLVERR_to_SLVERR"
                : "AXI_response_preserved_APB_PSLVERR_to_SLVERR";
  }
  void dedicated_dialects() {
    Json dialects = Json::object();
    const Json reset = "common_reset_cancels_transfers_and_pending_responses";
    if (uses(Protocol::Apb3))
      dialects["apb3"] = {{"ports", "no_PSTRB_no_PPROT_AxPROT_dropped"},
                          {"transfer", "one_SETUP_cycle_then_ACCESS_until_PREADY"},
                          {"read", lane_read},
                          {"reset", reset},
                          {"capacity", direction_capacity},
                          {"write", strobeless_write},
                          {"response", "PSLVERR_to_SLVERR_read_per_beat_write_merged_into_B"}};
    if (uses(Protocol::Apb2))
      dialects["apb2"] = {{"ports", "no_PSTRB_no_PPROT_no_PREADY_no_PSLVERR_AxPROT_dropped"},
                          {"write", strobeless_write},
                          {"transfer", "one_SETUP_then_one_ACCESS_cycle_no_target_error"},
                          {"read", lane_read},
                          {"reset", reset},
                          {"capacity", direction_capacity}};
    if (uses(Protocol::Axi4Lite))
      dialects["axi4_lite"] = {
          {"transfer", "one_single_beat_AW_W_or_AR_per_word_INCR_writes_pack_to_target_word"},
          {"write", "WSTRB_preserved_zero_strobe_word_written_with_WSTRB_zero"},
          {"read", lane_read},
          {"reset", reset},
          {"capacity", direction_capacity},
          {"response", "nonzero_BRESP_or_RRESP_to_SLVERR"},
          {"protection", "AxPROT_preserved"}};
    if (any(p.targets,
            [](const auto &t) { return t.data_bits == 64 && t.protocol == Protocol::Axi4Lite; }) ||
        (p.burst_data_bits == 64 && uses(Protocol::Axi4Lite)))
      dialects["axi4_lite"]["data_width"] =
          "32_or_64_bits_per_target_word_aligned_all_WSTRB_lanes_preserved";
    for (auto protocol : {Protocol::Apb3, Protocol::Apb2, Protocol::Axi4Lite})
      if (extended(protocol))
        dialects["address"] = address_contract();
    dialects["fixed"] = fixed_reject;
    contract()["dedicated_dialects"] = dialects;
    const auto previous = contract().value("attributes", std::string{});
    contract()["attributes"] = previous.starts_with("per_target_protocol_mapping_see_")
                                   ? previous + "_and_dedicated_dialects"
                                   : "per_target_protocol_mapping_see_dedicated_dialects";
    contract()["target_response"] = "AXI_response_preserved_dedicated_target_errors_to_SLVERR";
  }
  // Listed targets serve FIXED bursts; the others keep the SLVERR rule.
  void fixed_bursts() {
    const Json see = "SLVERR_every_beat_no_target_access_except_fixed_bursts_single_targets";
    for (const auto *entry : {"ahb_targets", "apb_targets", "dedicated_dialects"})
      if (contract().contains(entry))
        contract()[entry]["fixed"] = see;
    definition["fixed_bursts_single"] = names_if(p.targets, &TargetPlan::fixed_single);
    auto &fixed = contract()["fixed_bursts"];
    fixed = {{"reject", fixed_reject},
             {"single", "each_beat_served_alone_at_the_repeated_address_APB_and_AXI4_Lite_one_"
                        "word_aligned_access_AxSIZE_selects_only_strobes_and_read_lanes_AHB_NONSEQ_"
                        "SINGLE_transfers_split_by_write_strobes"},
             {"read", "each_beat_returns_its_own_result_and_data"},
             // Without error codes both failure causes answer the same code.
             {"write", p.error_codes
                           ? "all_W_beats_absorbed_one_B_first_failed_beat_code_else_OKAY"
                           : "all_W_beats_absorbed_one_B_SLVERR_if_any_beat_failed_or_the_start_is_"
                             "outside_the_window_else_OKAY"},
             // AXI4-Lite writes a beat without strobes with WSTRB zero; the text
             // changes only when an AXI4-Lite target serves FIXED bursts.
             {"strobes",
              any(p.targets,
                  [](const auto &t) { return t.fixed_single && t.protocol == Protocol::Axi4Lite; })
                  ? "zero_strobe_beat_no_access_at_APB_and_AHB_written_with_WSTRB_zero_at_AXI4_"
                    "Lite_APB3_APB2_partial_beat_SLVERR_no_access_later_beats_served"
                  : "zero_strobe_beat_skipped_APB3_APB2_partial_beat_SLVERR_no_access_later_"
                    "beats_served"},
             {"decode", "window_decode_first_every_beat_DECERR_outside_the_window"},
             {"wider_than_target", "SLVERR_no_target_access"},
             {"packets", "one_packet_never_cut"}};
    if (apb_widths.empty())
      return;
    fixed["single"] = "each_beat_at_repeated_address_APB_enabled_physical_words_in_32bit_"
                      "container_AXI4_Lite_one_word_access_AHB_NONSEQ_SINGLE";
    fixed["strobes"] = "zero_strobe_APB_AHB_quiet_Lite_WSTRB_zero_APB3_APB2_partial_physical_word_"
                       "no_access_prior_effects_kept_current_beat_stops_later_beats_served";
    fixed["wider_than_target"] = "wider_than_engine_word_SLVERR_APB_engine_word_is_32bits";
  }
  // Target timeouts, and AHB burst shaping listed only for targets that leave
  // a default; the others keep the packet rule of contract.ahb_targets.transfer.
  void target_options() {
    const auto timeouts = by_name(p.targets, [](const auto &t) {
      return !t.timeout ? std::nullopt
                        : std::optional{Json{{"cycles", t.timeout->cycles},
                                             {"completion", t.timeout->completion}}};
    });
    if (!timeouts.empty()) {
      definition["target_timeouts"] = timeouts;
      contract()["target_timeout"] = {
          {"measure", "target_clock_cycles_after_one_with_a_request_or_unanswered_burst_and_no_"
                      "response_offered_one_count_per_target_restarted_by_any_response"},
          {"expiry", "target_failed_until_common_reset"},
          {"responses", "none_or_with_completion_SLVERR_after_the_window_decode_no_rollback"},
          {"target_interface",
           "handshakes_held_until_completed_late_responses_discarded_a_held_read_burst_finishes_"
           "its_transfers_data_discarded_writes_get_no_further_data_nothing_else_starts"},
          {"status", "nl_t_<target>_timed_out_high_after_expiry"},
          {"not_detected", "stopped_target_clock_and_network_response_backpressure"},
          {"external_time_base", "unsupported"}};
    }
    const auto shaping = by_name(p.targets, [](const TargetPlan &t) -> std::optional<Json> {
      if (t.complete_writes || t.fixed_length)
        return Json{{"write_bursts", t.complete_writes ? "complete" : "packet"},
                    {"undefined_length_bursts", !t.fixed_length}};
      return std::nullopt;
    });
    if (shaping.empty())
      return;
    definition["ahb_bursts"] = shaping;
    contract()["ahb_bursts"] = {
        {"packet", "writes_INCR_for_incrementing_packets_SINGLE_for_WRAP_"
                   "partial_or_zero_strobes_switch_to_SINGLE_without_fixed_length_early_end"},
        {"complete", "writes_that_would_be_INCR4_8_16_go_out_as_INCR_WRAP_writes_as_SINGLE_no_"
                     "fixed_length_write_burst_ends_early_reads_unchanged"},
        {"undefined_length_bursts_false",
         "writes_SINGLE_reads_that_would_be_INCR_use_INCR16_INCR8_INCR4_and_SINGLE_blocks_within_"
         "the_packet_the_1KiB_boundary_and_the_window_end_HBURST_never_INCR"},
        {"both", "every_write_SINGLE_reads_in_blocks"},
        {"blocks", "a_packet_keeps_the_bus_across_its_blocks_the_next_block_NONSEQ_follows_"
                   "directly_when_ready_else_IDLE_BUSY_only_inside_a_block"}};
  }
  // The owner flags of a target's exclusive accesses, the low bit of their value first.
  Json id_flags(const ExclusiveTarget &x) const {
    auto out = Json::array();
    std::ranges::transform(x.id_flags, std::back_inserter(out),
                           [&](unsigned bit) { return p.user_flags.at(bit); });
    return out;
  }
  // Exclusive access support: listed only when some target supports it.
  void exclusive_access() {
    if (!any(p.targets, has_exclusive))
      return;
    const bool target_mode =
        any(p.targets, [](const auto &t) { return t.exclusive.mode == ExclusiveAccess::Target; });
    definition["exclusive_targets"] = by_name(p.targets, [&](const auto &t) {
      const auto &x = t.exclusive;
      return !has_exclusive(t) ? std::nullopt
             : x.mode == ExclusiveAccess::Target
                 ? std::optional{Json{{"mode", "target"}, {"id_flags", id_flags(x)}}}
                 : std::optional{Json{{"mode", "network"},
                                      {"monitors", x.monitors},
                                      {"granule", x.granule},
                                      {"id_flags", id_flags(x)}}};
    });
    contract()["exclusive"] = "per_target_see_exclusive_targets_other_targets_AxLOCK_"
                              "ignored_normal_access_OKAY_never_EXOKAY";
    Json rules = {
        {"admission", "exclusive_to_listed_target_1_2_4_8_or_16_beats_aligned_to_total_bytes_not_"
                      "FIXED_else_SLVERR_no_target_access_AXI_A7_2_4"},
        {"limit", "min_of_128_bytes_2_pow_len_target_words_own_window_AHB_or_target_mode_at_most_"
                  "16_target_words_"
                  "converted_WRAP_at_most_16_target_words_"
                  "in_target_mode_len_AXI4_target_AxLEN_width_else_network_length_field"},
        {"key", "exclusive_id_flags"},
        {"weft", "request_lock_response_exclusive_okay_failure_wins_when_merged"},
        {"response", "exclusive_read_EXOKAY_per_successful_beat_exclusive_write_EXOKAY_when_it_"
                     "updated_the_target_OKAY_when_it_failed"},
        {"packets", "an_exclusive_burst_is_never_cut"},
        {"cache", "AxCACHE_not_checked_system_must_route_exclusive_accesses_to_the_monitor"},
        {"network",
         "monitor_in_the_target_outlet_reservation_per_key_granule_rounded_region_any_write_"
         "reaching_the_target_clears_overlapping_reservations_a_write_reaching_the_target_in_"
         "flight_from_descriptor_capture_to_B_leaves_an_exclusive_read_unrecorded_and_its_key_s_"
         "old_reservation_kept_an_exclusive_read_records_at_its_request_whatever_it_answers_"
         "failed_exclusive_write_zero_strobes_and_data_AXI4_AXI4_Lite_write_APB_AHB_no_transfer_"
         "OKAY_full_table_LFSR_replacement_target_must_have_no_other_write_path"},
        {"strobeless", "APB3_APB2_network_exclusive_beats_narrower_than_4_bytes_SLVERR_at_source"}};
    if (target_mode)
      rules["target"] = "AxLOCK_passed_target_ID_flow_in_low_bits_exclusive_owner_flags_above_flow_"
                        "first_flag_in_byte_order_at_top_target_EXOKAY_"
                        "preserved_for_exclusive_bursts";
    contract()["exclusive_access"] = rules;
    if (target_mode && contract().value("target_response", std::string{}) ==
                           "OKAY_SLVERR_DECERR_preserved_EXOKAY_rejected")
      contract()["target_response"] =
          "OKAY_SLVERR_DECERR_preserved_EXOKAY_preserved_for_exclusive_bursts_to_"
          "exclusive_target_targets_else_rejected";
    for (const auto &t : p.targets)
      if (t.exclusive.mode == ExclusiveAccess::Target && t.outstanding < 2)
        definition["target_id_bits"][t.name] = t.id_bits.value();
  }
  // Multiple outstanding bursts: listed only when some source or target
  // keeps more than one burst in flight.
  void outstanding() {
    if (!multi_source && !multi_target)
      return;
    definition["outstanding"] = {{"initiators", field_by_name(p.sources, &SourcePlan::outstanding)},
                                 {"targets", field_by_name(p.targets, &TargetPlan::outstanding)}};
    contract()["capacity"] =
        "source_bursts_per_direction_as_many_transactions_shared_by_reads_and_writes_target_"
        "bursts_shared_by_reads_and_writes_see_outstanding";
    if (multi_source) {
      // Full AXI lists its table bound; Lite appears only when its table limits the two keys.
      definition["outstanding"]["initiator_id_tables"] = by_name(p.sources, [](const auto &s) {
        return s.outstanding > 1 && s.id_keys &&
                       (!(s.protocol == Protocol::Axi4Lite && !s.apb) || s.id_table < s.id_keys)
                   ? std::optional{s.id_table}
                   : std::nullopt;
      });
      auto &ordering = contract()["ordering"];
      ordering = {
          {"destination", "every_burst_of_one_AXI_ID_in_flight_on_a_source_direction_goes_to_one_"
                          "target_or_to_local_completion_different_IDs_to_any_targets"},
          {"new_destination", "a_burst_whose_ID_is_bound_to_another_destination_waits_after_its_"
                              "address_handshake_until_the_last_response_of_that_ID_has_"
                              "reached_the_source_holding_the_request_merger_once_chosen"},
          {"id_table", "initiator_id_tables_entries_of_direction_and_AXI_ID_shared_by_reads_and_"
                       "writes_a_burst_with_a_new_key_waits_at_the_request_merger_holding_it_"
                       "while_every_entry_is_busy"},
          {"responses", "same_ID_in_issue_order_different_IDs_in_any_order_with_the_original_"
                        "AxID_steered_by_the_Weft_tag_no_reorder_storage"},
          {"decode_miss", "DECERR_from_the_default_target_without_target_access"},
          {"local_errors", "admission_and_burst_shape_errors_complete_locally_as_their_own_"
                           "destination_after_every_earlier_burst_of_their_ID"},
          {"write_data", "each_burst_AW_then_its_W_beats_contiguous_never_interleaved"},
          {"read_data", "in_network_arrival_order_bursts_of_different_IDs_may_interleave_"
                        "beat_by_beat_bursts_of_one_ID_never_interleave"},
          {"liveness", "head_of_line_blocking_a_burst_waiting_for_its_ID_or_an_ID_entry_holds_"
                       "every_later_burst_of_its_direction"}};
      // The keys below appear only where their feature stands next to several
      // bursts per source.
      if (!h.plan.cuts.empty())
        ordering["crossings"] =
            "every_crossing_FIFO_keeps_its_words_in_order_and_carries_the_response_tag";
      if (any(p.targets, has_exclusive))
        ordering["exclusive"] =
            "monitors_key_on_exclusive_id_flags_exclusive_bursts_follow_the_per_ID_order";
    }
    if (multi_target)
      target_outstanding();
  }
  // Targets with several bursts in flight. A dedicated AXI4-Lite target has
  // no ID port to widen; the AXI4 ID records cover the AXI4 targets.
  void target_outstanding() {
    bool widened = false;
    for (const auto &t : p.targets)
      if (t.outstanding > 1 && t.protocol != Protocol::Axi4Lite) {
        definition["target_id_bits"][t.name] = t.id_bits.value();
        widened = true;
      }
    const auto lite_multi = any(p.targets, [](const auto &t) {
      return t.outstanding > 1 && t.protocol == Protocol::Axi4Lite;
    });
    if (!widened && !lite_multi)
      return;
    auto &pool = contract()["target_outstanding"];
    if (widened)
      pool = {{"pool", "reads_and_writes_share_the_capacity_a_burst_holds_an_entry_from_its_head_"
                       "until_the_cycle_after_its_last_response_word_leaves_a_head_enters_only_"
                       "below_the_capacity"},
              {"reads", "AR_in_arrival_order_an_R_belongs_to_the_oldest_read_of_its_RID_not_yet_"
                        "received_a_burst_beats_arrive_together_behind_a_width_merge_reads_in_"
                        "flight_share_one_source_and_AxID"},
              {"writes", "AW_then_all_W_beats_per_burst_in_arrival_order_B_matched_to_the_oldest_"
                         "burst_of_its_target_ID"},
              {"unexpected_response_id", "BREADY_takes_only_a_B_naming_the_oldest_write_of_its_"
                                         "target_ID_other_B_wait_an_R_with_another_RID_mid_burst_"
                                         "or_naming_no_read_answers_SLVERR_for_its_beat"},
              {"local_completion", "window_decode_misses_answer_DECERR_writes_before_any_later_"
                                   "write_of_the_same_target_ID_reads_once_every_earlier_read_is_"
                                   "received"},
              {"packets", "each_packet_uses_one_pool_entry_same_source_tag_keeps_separate_ordered_"
                          "contexts_source_burst_capacity_and_four_packet_splitter_limit_are_"
                          "independent"}};
    if (lite_multi)
      pool["dedicated_lite"] =
          "packets_of_one_direction_in_flight_up_to_the_capacity_words_of_a_burst_issue_back_"
          "to_back_without_waiting_for_B_or_R_a_direction_change_waits_for_every_earlier_"
          "packet_response_one_B_after_the_last_words_B";
    if (widened && p.per_target)
      pool["merge_contexts"] =
          "K_packet_contexts_per_direction_B_restores_oldest_matching_source_ID_tag_"
          "R_FIFO_owns_shared_gather_until_final_upstream_beat_same_tag_packets_are_distinct";
  }
  // Without error codes every contract entry reports the encoding the sources actually observe.
  void error_encoding() {
    if (p.error_codes)
      return;
    decerr_as_slverr(contract());
    if (contract().contains("target_response"))
      contract()["target_response"] = "OKAY_preserved_every_target_error_to_SLVERR";
  }
  void clock_record() {
    for (const auto *key : {"domain", "clock_port", "reset_port", "test_port", "gated"})
      definition.erase(key);
    if (!p.placement->roots.empty()) {
      definition["clock_roots"] = Json::array();
      for (const auto &root : p.placement->roots) {
        Json record = {{"name", root.name}, {"clock_port", root.clock_port}};
        if (root.period_ps)
          record["period_ps"] = *root.period_ps;
        definition["clock_roots"].push_back(std::move(record));
      }
    }
    definition["clock_domains"] = Json::array();
    for (const auto &domain : h.plan.domains) {
      Json record = {{"name", domain.name},
                     {"clock_port", domain.clock_port},
                     {"reset_port", domain.reset_port}};
      if (domain.period_ps)
        record["period_ps"] = *domain.period_ps;
      domain_gating(record, domain);
      definition["clock_domains"].push_back(std::move(record));
    }
    definition["synchronizer_stages"] = h.plan.synchronizer_stages;
    if (!p.placement->crossings.empty()) {
      definition["clock_crossings"] = Json::array();
      for (const auto &crossing : p.placement->crossings) {
        Json record = {{"from_domain", crossing.from_domain}, {"to_domain", crossing.to_domain}};
        if (const auto *async = std::get_if<AsyncClockCrossing>(&crossing.options)) {
          if (async->depth)
            record["depth"] = *async->depth;
          if (async->synchronizer_stages)
            record["synchronizer_stages"] = *async->synchronizer_stages;
          if (async->reset_handshake)
            record["reset_handshake"] = *async->reset_handshake;
        } else if (const auto *sync = std::get_if<SyncClockCrossing>(&crossing.options)) {
          record["kind"] = "related";
          if (sync->depth)
            record["depth"] = *sync->depth;
          if (sync->fast)
            record["fast"] = true;
        } else {
          record["kind"] = "edge_aligned";
          if (!std::get<EdgeAlignedCrossing>(crossing.options).slow_pipeline)
            record["slow_pipeline"] = false;
        }
        definition["clock_crossings"].push_back(std::move(record));
      }
    }
    auto &domains = definition["endpoint_clock_domains"];
    for (const auto &[object, domain] : h.plan.object_domains)
      if (object.starts_with("source/") || object.starts_with("target/"))
        domains[object[0] == 's' ? "initiators" : "targets"][object.substr(7)] = domain;
    const bool any_handshake = std::ranges::any_of(h.plan.cuts, [](const ClockCut &cut) {
      const auto *async = std::get_if<AsyncClockCut>(&cut.options);
      return async && async->reset_handshake;
    });
    contract()["reset"] =
        any_handshake
            ? "per_domain_async_assert_port_release_crossing_side_resets_alone_with_handshake_"
              "other_side_drops_queued_words_in_flight_transactions_not_recovered"
            : "per_domain_async_assert_port_release_crossing_sides_assert_together_release_any_"
              "order";
    const bool any_related = std::ranges::any_of(h.plan.cuts, [](const ClockCut &cut) {
      return !std::holds_alternative<AsyncClockCut>(cut.options);
    });
    const bool any_async = std::ranges::any_of(h.plan.cuts, [](const ClockCut &cut) {
      return std::holds_alternative<AsyncClockCut>(cut.options);
    });
    contract()["crossing"] = any_related ? "typed_clock_adapters_common_reset_full_fields"
                             : p.reset_coverage == ResetCoverage::All
                                 ? "async_fifo_gray_count_receive_register_reset_payload"
                                 : "async_fifo_gray_count_receive_register_no_reset_payload";
    contract()["physical_obligations"] =
        any_related
            ? std::string("OPEN_related_static_edge_data_paths_and_reset_order") +
                  (any_async
                       ? "_gray_count_skew_slot_read_timing_synchronizer_placement_and_reliability"
                       : "")
            : std::string("OPEN_gray_count_skew_slot_read_timing_reset_order_synchronizer_"
                          "placement_and_reliability") +
                  (any_handshake ? "_one_side_reset_held_n_plus_2_cycles_of_the_other_side_clock"
                                 : "");
  }
  // Reset, test, enable, ungate and static bypass ports have no owning clock; a clock port owns
  // itself.
  std::string port_clock(const Port &port) const {
    const auto separator = port.endpoint.find(':');
    const auto kind = port.endpoint.substr(0, separator);
    if (kind == "reset" || kind == "test" || kind == "enable" || kind == "ungate" ||
        kind == "bypass" || kind == "domain")
      return kind == "domain" ? port.name : "";
    if (!h.plan.clocked())
      return p.domain.clock_port;
    const auto name = port.endpoint.substr(separator + 1);
    const auto &object =
        kind == "address_input"
            ? std::ranges::find(p.address_inputs, name, &AddressInput::name)->clock_domain
        : kind == "error_log"
            ? h.plan.observer_domain(name)
            : h.plan.object_domains.at((kind == "initiator" ? "source/" : "target/") + name);
    return std::ranges::find(h.plan.domains, object, &DomainPlan::name)->clock_port;
  }
  Json map_record() const {
    const auto hardware_nodes = [&](const char *direction) {
      Json ids = Json::array();
      std::ranges::transform(topology[direction]["nodes"], std::back_inserter(ids),
                             [](const Json &node) { return node["hardware_node"]; });
      return ids;
    };
    Json map = {{"format_version", 1},
                {"generator", "NocLoom"},
                {"module", h.ir.name},
                {"definition", definition},
                {"ports", Json::array()},
                {"network",
                 {{"read_write", "merged"},
                  {"request_nodes", hardware_nodes("request")},
                  {"response_nodes", hardware_nodes("response")},
                  {"adapters", Json::array()},
                  {"resources", Json::array()}}}};
    for (const auto &port : h.ir.ports)
      map["ports"].push_back({{"name", port.name},
                              {"direction", port.input ? "input" : "output"},
                              {"width", port.width},
                              {"endpoint", port.endpoint},
                              {"signal", port.signal},
                              {"clock", port_clock(port)}});
    auto &network = map["network"];
    for (const auto &adapter : h.plan.adapters())
      network["adapters"].push_back({{"name", adapter.name},
                                     {"segment", adapter.segment},
                                     {"kind", adapter.serializer ? "serializer" : "deserializer"},
                                     {"direction", adapter.response ? "response" : "request"},
                                     {"wide_bytes", adapter.wide_bytes},
                                     {"narrow_bytes", adapter.narrow_bytes}});
    network["interleaved_targets"] = Json::array();
    for (const auto &target : h.plan.targets)
      if (interleaved_target(h.plan, target))
        network["interleaved_targets"].push_back(target.name);
    for (const auto &resource : h.plan.graph.resources) {
      Json row{
          {"name", resource.id}, {"role", resource.role}, {"payload_bits", resource.payload_bits}};
      if (resource.pipe && (resource.pipe->half_rate || !resource.pipe->bypass_port.empty() ||
                            resource.pipe->ungated)) {
        row["capacity"] = resource.capacity;
        if (resource.pipe->half_rate)
          row["half_rate"] = true;
        if (!resource.pipe->bypass_port.empty()) {
          row["bypass_port"] = resource.pipe->bypass_port;
          row["bypass_forward"] = true;
        }
        if (resource.pipe->ungated)
          row["ungated"] = true;
      }
      network["resources"].push_back(std::move(row));
    }
    for (bool response : {false, true}) {
      auto &routes = network[response ? "response_routes" : "request_routes"] = Json::array();
      const auto &graph = response ? h.plan.exchange.response : h.plan.exchange.request;
      std::ranges::transform(graph.routes, std::back_inserter(routes), [](const auto &r) {
        Json row{{"entry", r.entry}, {"exit", r.exit}, {"hardware_nodes", r.nodes}};
        if (!r.flows.empty())
          row["source_paths"] = r.flows;
        if (r.target_path)
          row["target_path"] = *r.target_path;
        return row;
      });
    }
    network["admission"] = Json::array();
    network["formal"]["sources"] = Json::object();
    for (const auto &source : names(p.sources)) {
      const auto prefix = "source/" + source + "/";
      Json admission{{"source", source},
                     {"write_route_match", value_ref(h.bindings.at(prefix + "wr.route_mapped"))},
                     {"read_route_match", value_ref(h.bindings.at(prefix + "rr.route_mapped"))}};
      // The target index each direction's requests carry (message.hpp point 16).
      for (const std::string dir : {"write", "read"})
        if (const auto at = h.bindings.find(prefix + dir + ".route_target"); at != h.bindings.end())
          admission[dir + "_route_target"] = value_ref(at->second);
      // A mode-dependent decode: the plane of each direction's burst in service.
      for (const auto *dir : {"wr", "rr"})
        if (const auto at = h.bindings.find(prefix + dir + ".plane_now"); at != h.bindings.end())
          admission[std::string(*dir == 'w' ? "write" : "read") + "_mode_plane"] =
              value_ref(at->second);
      network["admission"].push_back(std::move(admission));
      network["formal"]["sources"][source] = formal_anchors(prefix, source_anchors);
    }
    for (const auto &flag : p.address_selectors)
      network["address_selectors"][flag.name] = value_ref(h.bindings.at("mode/" + flag.name));
    if (p.level_hint_bits())
      scheduling_anchors(network);
    network["formal"]["targets"] = Json::object();
    for (const auto &target : names(p.targets))
      network["formal"]["targets"][target] =
          formal_anchors("burst_service/" + target + "/", target_anchors);
    network["storage"] = Json::array();
    for (const auto &store : h.storage)
      network["storage"].push_back({{"resource", store.resource},
                                    {"payload", value_refs(store.payload)},
                                    {"control", value_refs(store.control)},
                                    {"acquire", value_ref(store.acquire)},
                                    {"release", value_ref(store.release)}});
    return map;
  }
  void scheduling_anchors(Json &network) const {
    for (const auto &[key, value] : h.bindings) {
      if (!key.starts_with("graph/") || value.index >= h.ir.emitted.value_or(h.ir.values.size()))
        continue;
      const auto split = key.rfind('.');
      if (split == std::string::npos)
        continue;
      const auto suffix = key.substr(split + 1);
      if (suffix.starts_with("valid") || suffix.starts_with("level") || suffix == "held" ||
          suffix == "winner" || suffix == "ready" || suffix == "acquire" || suffix == "transfer")
        network["formal"]["scheduling"][key.substr(0, split)][suffix] = value_ref(value);
    }
  }
  // Observation anchors for the connectivity proof bench
  // (tests/run_connectivity_formal.py): value/ handles into the netlist, emitted only
  // where the binding exists in this network and names emitted hardware.
  Json formal_anchors(const std::string &prefix, std::span<const Anchor> anchors) const {
    Json entry = Json::object();
    for (const auto &[key, suffix] : anchors) {
      const auto it = h.bindings.find(prefix + suffix);
      if (it != h.bindings.end() && it->second.index < h.ir.emitted.value_or(h.ir.values.size()))
        entry[key] = value_ref(it->second);
    }
    return entry;
  }
  // Each clock gate: its switch, or its endpoint and zone; the idle and wake terms behind its
  // enable; its registers.
  void clock_gates(Json &network, Json &semantics) const {
    for (auto it = h.bindings.lower_bound("gate/");
         it != h.bindings.end() && it->first.starts_with("gate/"); ++it) {
      if (!it->first.ends_with(".clock"))
        continue;
      const auto stem = it->first.substr(0, it->first.size() - 6), object = stem.substr(5);
      const auto owner = gate_owner(object);
      Json record = {
          {"clock_domain", h.plan.clocked() ? h.plan.object_domains.at(owner) : p.domain.name},
          {"registers", std::ranges::count_if(h.ir.states, [&](const State &s) {
             return s.clock.index == it->second.index;
           })}};
      if (owner.starts_with("req/") || owner.starts_with("rsp/"))
        record["switch"] = owner.substr(0, 4) + h.plan.node(owner.substr(4)).name;
      else {
        record[owner.starts_with("source/") ? "initiator" : "target"] =
            owner.substr(owner.find('/') + 1);
        record["zone"] = owner == object ? "transport" : "protocol";
        if (const auto mode = p.endpoint_gating(owner)) {
          record["mode"] =
              std::array{"separate", "shared", "transport_only"}[static_cast<int>(*mode)];
          record["zones"] = *mode == EndpointClockGating::Shared
                                ? Json::array({"protocol", "transport"})
                                : Json::array({record["zone"]});
          if (*mode == EndpointClockGating::Shared)
            record["zone"] = "shared";
        }
      }
      for (const auto *key : {"enable", "idle", "wake", "remaining"})
        record[key] = value_ref(h.bindings.at(stem + "." + key));
      network["clock_gates"].push_back(std::move(record));
    }
    const auto endpoints = [&](const auto &list, const std::string &kind) {
      for (const auto &e : list)
        if (e.clock_gating) {
          const auto count = std::ranges::count_if(network["clock_gates"], [&](const auto &g) {
            return g.contains(kind) && g[kind] == e.name;
          });
          network["endpoint_clock_gating"][kind][e.name] = {
              {"mode", std::array{"separate", "shared",
                                  "transport_only"}[static_cast<int>(*e.clock_gating)]},
              {"gate_count", count},
              {"protocol_clock",
               *e.clock_gating == EndpointClockGating::TransportOnly ? "domain" : "gated"}};
          if (*e.clock_gating != EndpointClockGating::Separate)
            semantics["endpoint_clock_gating"][kind][e.name] =
                network["endpoint_clock_gating"][kind][e.name]["mode"];
        }
    };
    endpoints(p.sources, "initiator");
    endpoints(p.targets, "target");
  }
  // Each cell's binding (none: behavioral) and the path of every library cell instance below the
  // module, or of every wrapper instance of an unbound cell.
  void cells(Json &map) const {
    const auto &g = p.cells.clock_gate;
    const auto &s = p.cells.synchronizer;
    auto ties = [](const std::map<std::string, bool> &tie) {
      Json out = Json::object();
      for (const auto &[pin, value] : tie)
        out[pin] = value ? 1 : 0;
      return out;
    };
    Json gate = g ? Json{{"module", g->module},
                         {"clock", g->clock},
                         {"enable", g->enable},
                         {"output", g->output},
                         {"tie", ties(g->tie)}}
                  : Json::object();
    if (g && g->test)
      gate["test"] = *g->test;
    Json sync = s ? Json{{"module", s->module},
                         {"stages", s->stages},
                         {"clock", s->clock},
                         {"data", s->data},
                         {"reset", s->reset},
                         {"output", s->output},
                         {"reset_active", s->reset_active_low ? "low" : "high"},
                         {"tie", ties(s->tie)}}
                  : Json::object();
    gate["instances"] = sync["instances"] = Json::array();
    for (std::size_t i = 0; i < h.ir.emitted.value_or(h.ir.values.size()); ++i)
      if (h.ir.values[i].op == Op::ClockGate)
        gate["instances"].push_back("nl_internal_v" + decimal(i) + "_gate" + (g ? "/u_cell" : ""));
    const bool chained = s && s->stages == 1;
    for (const auto &c : synchronizer_chains(h.ir))
      for (unsigned b = 0; b < c.width; ++b)
        for (unsigned k = 0; k < (chained ? c.stages : 1); ++k)
          sync["instances"].push_back(c.instance(b) + (chained
                                                           ? "/u_stage[" + decimal(k) + "].u_cell"
                                                       : s ? "/u_cell"
                                                           : ""));
    map["cells"] = {{"clock_gate", gate}, {"synchronizer", sync}};
  }
  void clocked_network(Json &network) const {
    const auto clock_of = [&](ValueId id) {
      return h.ir.ports.at(h.ir.values.at(root_clock(h.ir, id).index).input_port.index).name;
    };
    for (std::size_t i = 0; i < h.plan.graph.resources.size(); ++i) {
      network["resources"][i]["clock_domain"] = h.plan.graph.resources[i].domain;
      network["resources"][i]["capacity"] = h.plan.graph.resources[i].capacity;
    }
    for (std::size_t i = 0; i < h.storage.size(); ++i)
      network["storage"][i]["clock"] = clock_of(h.storage[i].clock);
    for (std::size_t i = 0; const auto &adapter : h.plan.adapters())
      network["adapters"][i++]["clock_domain"] = adapter.domain;
    auto &cdc = network["cdc"];
    cdc = {{"objects", h.plan.object_domains},
           {"cuts", Json::array()},
           {"registers", Json::array()},
           {"synchronizer_stages", Json::array()},
           {"physical_status", "OPEN"}};
    for (const auto &cut : h.plan.cuts)
      cdc["cuts"].push_back(crossing_record(cut));
    // A synchronizer stage is register k of its chain's synchronizer instances.
    std::map<std::size_t, Json> synchronized;
    for (const auto &c : synchronizer_chains(h.ir)) {
      Json instances = Json::array();
      for (unsigned b = 0; b < c.width; ++b)
        instances.push_back(c.instance(b));
      for (auto [k, i] = std::pair{c.stages, c.tail}; k-- > 0;
           i = h.ir.states[h.ir.values[i].state.index].next.index)
        synchronized[i] = {{"instances", instances}, {"stage", k}};
    }
    // Logic kept only for observation (a pressure register no arbiter reads) is not emitted.
    const auto emitted = h.ir.emitted.value_or(h.ir.values.size());
    for (const auto &state : h.ir.states) {
      if (state.output.index >= emitted)
        continue;
      Json record = {{"value", value_ref(state.output)}, {"next", value_ref(state.next)}};
      if (const auto sync = synchronized.find(state.output.index); sync != synchronized.end())
        record["synchronizer"] = sync->second;
      else
        record["rtl"] = "nl_internal_v" + decimal(state.output.index);
      record.update({{"width", h.ir.values.at(state.output.index).width},
                     {"clock", clock_of(state.clock)},
                     {"async_reset", state.reset.has_value()}});
      if (state.reset)
        record["reset"] = {{"signal", value_ref(state.reset->signal)},
                           {"value", state.reset->value}};
      cdc["registers"].push_back(std::move(record));
    }
    for (auto stage : h.ir.synchronizer_stages)
      if (stage.index < emitted)
        cdc["synchronizer_stages"].push_back(value_ref(stage));
  }
  // One crossing FIFO with the physical obligations it leaves open.
  Json crossing_record(const ClockCut &cut) const {
    if (!std::holds_alternative<AsyncClockCut>(cut.options))
      return related_crossing_record(cut);
    const auto &options = std::get<AsyncClockCut>(cut.options);
    const auto store = std::ranges::find(h.storage, cut.name, &CrossStorageBinding::resource);
    const auto receive =
        std::ranges::find(h.storage, cut.name + "/receive", &CrossStorageBinding::resource);
    const auto ref = [&](const std::string &binding) { return value_ref(h.bindings.at(binding)); };
    const auto depth = options.depth.depth;
    const std::span<const ValueId> slots(store->payload);
    // Slots are stored field-major: depth slots of field 0, then field 1.
    Json fields = Json::array();
    for (std::size_t i = 0; i < cut.fields.size(); ++i)
      fields.push_back({{"name", cut.fields[i].first},
                        {"width", cut.fields[i].second},
                        {"storage", value_refs(slots.subspan(i * depth, depth))},
                        {"received", value_ref(receive->payload.at(i))},
                        {"input", ref(cut.name + "/input.field" + decimal(i))},
                        {"output", ref(cut.name + "/output.field" + decimal(i))}});
    // Control layout: sender {write_index, write_count, read_count_sync...};
    // receiver {read_index, read_count, out_valid, write_count_sync...}; each side then
    // {announce, on, wait, announce_sync..., acknowledge_sync...} with the reset handshake.
    const std::size_t n = options.synchronizer_stages;
    const std::span<const ValueId> sender(store->control), receiver(receive->control);
    const auto read_sync = sender.subspan(2, n), write_sync = receiver.subspan(3, n);
    Json obligations = Json::array({{{"kind", "gray_count_skew"},
                                     {"source", value_ref(store->control.at(1))},
                                     {"destination", value_ref(write_sync.front())},
                                     {"bound", "bit_to_bit_skew_below_both_clock_periods"}},
                                    {{"kind", "gray_count_skew"},
                                     {"source", value_ref(receive->control.at(1))},
                                     {"destination", value_ref(read_sync.front())},
                                     {"bound", "bit_to_bit_skew_below_both_clock_periods"}},
                                    {{"kind", "slot_read_max_delay"},
                                     {"sources", value_refs(store->payload)},
                                     {"destinations", value_refs(receive->payload)},
                                     {"bound", "below_write_count_synchronization_latency"}}});
    static const char *const depth_sources[] = {"formula", "fallback", "explicit"};
    Json record = {{"name", cut.name},
                   {"segment", cut.segment},
                   {"from_domain", cut.from_domain},
                   {"to_domain", cut.to_domain},
                   {"depth", depth},
                   {"depth_source", depth_sources[static_cast<int>(options.depth.source)]},
                   {"count_bits", options.count_bits},
                   {"index_bits", options.index_bits},
                   {"synchronizer_stages", options.synchronizer_stages},
                   {"fields", fields},
                   {"write_index", value_ref(store->control.at(0))},
                   {"write_count", value_ref(store->control.at(1))},
                   {"read_count_sync", value_refs(read_sync)},
                   {"read_index", value_ref(receive->control.at(0))},
                   {"read_count", value_ref(receive->control.at(1))},
                   {"receive_valid", value_ref(receive->control.at(2))},
                   {"write_count_sync", value_refs(write_sync)},
                   {"write", value_ref(store->acquire)},
                   {"read", value_ref(store->release)},
                   {"obligations", obligations}};
    for (const std::string end : {"input", "output"})
      for (const std::string signal : {"valid", "ready"})
        record[end + "_" + signal] = ref(cut.name + "/" + end + "." + signal);
    if (!options.reset_handshake)
      return record;
    const auto handshake = [&](std::span<const ValueId> side) {
      return Json{{"announce", value_ref(side[0])},
                  {"on", value_ref(side[1])},
                  {"wait", value_ref(side[2])},
                  {"announce_sync", value_refs(side.subspan(3, n))},
                  {"acknowledge_sync", value_refs(side.subspan(3 + n, n))}};
    };
    record["reset_handshake"] = true;
    record["obligations"].push_back(
        {{"kind", "one_side_reset_hold"},
         {"bound", "reset_alone_held_" + decimal(n + 2) + "_cycles_of_the_other_side_clock"}});
    record["handshake"] = {{"sender", handshake(sender.subspan(2 + n))},
                           {"receiver", handshake(receiver.subspan(3 + n))}};
    return record;
  }
  Json related_crossing_record(const ClockCut &cut) const {
    const auto store = std::ranges::find(h.storage, cut.name, &CrossStorageBinding::resource);
    const auto receive =
        std::ranges::find(h.storage, cut.name + "/receive", &CrossStorageBinding::resource);
    const auto ref = [&](const std::string &binding) { return value_ref(h.bindings.at(binding)); };
    const bool edge_aligned = std::holds_alternative<EdgeAlignedCut>(cut.options);
    const auto depth = edge_aligned ? 0U : std::get<SyncClockCut>(cut.options).depth.depth;
    const std::span<const ValueId> slots(store->payload);
    Json fields = Json::array();
    for (std::size_t i = 0; i < cut.fields.size(); ++i) {
      Json field = {{"name", cut.fields[i].first},
                    {"width", cut.fields[i].second},
                    {"storage", value_refs(slots.subspan(i * depth, depth))},
                    {"input", ref(cut.name + "/input.field" + decimal(i))},
                    {"output", ref(cut.name + "/output.field" + decimal(i))}};
      fields.push_back(std::move(field));
    }
    Json record = {{"name", cut.name},
                   {"segment", cut.segment},
                   {"kind", edge_aligned ? "edge_aligned" : "sync"},
                   {"from_domain", cut.from_domain},
                   {"to_domain", cut.to_domain},
                   {"depth", depth},
                   {"fields", fields},
                   {"write", value_ref(store->acquire)},
                   {"read", value_ref(store->release)},
                   {"obligations", Json::array({{{"kind", "related_clock_data_path"},
                                                 {"sources", value_refs(store->payload)},
                                                 {"destinations", value_refs(receive->payload)},
                                                 {"bound", "common_root_static_edge_timing"}}})}};
    for (const std::string end : {"input", "output"})
      for (const std::string signal : {"valid", "ready"})
        record[end + "_" + signal] = ref(cut.name + "/" + end + "." + signal);
    if (edge_aligned) {
      const auto &options = std::get<EdgeAlignedCut>(cut.options);
      record["slow_pipeline"] = options.slow_pipeline;
      record["slow_from_sender"] = options.slow_from_sender;
      if (options.slow_from_sender || !options.slow_pipeline)
        record["admission_open"] = ref(cut.name + "/admission_open");
      const auto first = 0U;
      if (store->payload.size() > first)
        record["slow_pipeline_storage"] = value_refs(std::span(store->payload).subspan(first));
      if (!receive->payload.empty())
        record["slow_pipeline_storage"] = value_refs(receive->payload);
    } else {
      const auto &options = std::get<SyncClockCut>(cut.options);
      static const char *const depth_sources[] = {"formula", "fallback", "explicit"};
      record["depth_source"] = depth_sources[static_cast<int>(options.depth.source)];
      record["count_bits"] = options.count_bits;
      record["index_bits"] = options.index_bits;
      record["fast"] = options.fast;
      record["counter_return_isolation"] = options.fast ? "direct_related" : "local_register";
      record["write_index"] = value_ref(store->control.at(0));
      record["write_count"] = value_ref(store->control.at(1));
      record["read_index"] = value_ref(receive->control.at(0));
      record["read_count"] = value_ref(receive->control.at(1));
      record["receive_valid"] = ref(cut.name + "/output.valid");
      record["read_count_return"] = ref(cut.name + "/read_count_return");
      record["write_count_return"] = ref(cut.name + "/write_count_return");
      if (options.fast)
        record["obligations"].push_back(
            {{"kind", "fast_credit_counter_return"}, {"bound", "related_clock_fast_paths_timed"}});
    }
    return record;
  }
  // A swappable cell model file instantiated by the module (the clock gate) joins the sources.
  ArtifactSet artifacts(const Json &map) const {
    const auto &top = h.ir.name;
    const auto rtl = top + ".v", mapping = top + ".map.json", manifest = top + ".bundle.json",
               dependency = top + ".dependency.json";
    // The map holds the effective definition used by both output records.
    Json bundle = {{"format_version", 1},
                   {"generator", "NocLoom"},
                   {"version", version()},
                   {"definition", map.at("definition")},
                   {"language", "verilog_2001"},
                   {"top", top},
                   {"files",
                    {{{"path", rtl}, {"role", "rtl"}},
                     {{"path", mapping}, {"role", "interface_map"}},
                     {{"path", dependency}, {"role", "dependency_graph"}},
                     {{"path", manifest}, {"role", "manifest"}}}},
                   {"synthesis_sources", {rtl}},
                   {"simulation_sources", {rtl}}};
    ArtifactSet out{{rtl, emit_verilog(h.ir)}, {mapping, map.dump(2) + "\n"}};
    out.push_back({dependency, dependency_graph_json(p.dependency, p.dependency_report, top,
                                                     "NocLoom", fnv1a64(out[0].text),
                                                     fnv1a64(map.at("definition").dump()))});
    // Cell wrappers precede the module in the sources; bound library cell models stay out.
    for (const auto &cell : cell_files(h.ir, p.cells)) {
      const bool bound = cell.path == "nocloom_clock_gate.v" ? p.cells.clock_gate.has_value()
                                                             : p.cells.synchronizer.has_value();
      bundle["files"].push_back({{"path", cell.path},
                                 {"role", cell.library ? "library_cell_model"
                                          : bound      ? "cell_wrapper"
                                                       : "cell_model"}});
      if (cell.library)
        bundle["library_cell_models"].push_back(cell.path);
      for (const auto *key : {"synthesis_sources", "simulation_sources"})
        if (!cell.library)
          bundle[key].insert(bundle[key].end() - 1, cell.path);
      out.push_back({cell.path, cell.text});
    }
    out.push_back({manifest, bundle.dump(2) + "\n"});
    std::ranges::sort(out, {}, &Artifact::path);
    return license_output_json(std::move(out));
  }
};
} // namespace

Result<ArtifactSet> emit_axi4_network(const NetworkPlan &p, RegisterReset register_reset) {
  auto lowered = lower_axi4_exchange_composition(p, register_reset);
  if (!lowered)
    return Result<ArtifactSet>::failure(lowered.diagnostics());
  return MapWriter(p, lowered.value()).run();
}
} // namespace nocloom::detail
