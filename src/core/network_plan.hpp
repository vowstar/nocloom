#pragma once
#include "async_fifo.hpp"
#include "dependency_graph.hpp"
#include "error_logger.hpp"
#include "exclusive.hpp"
#include "id_class.hpp"
#include "register_bank.hpp"
#include "switch_graph.hpp"
#include "treadle.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <iterator>
#include <map>
#include <nocloom/definition.hpp>
#include <numeric>
#include <set>
#include <stdexcept>

namespace nocloom::detail {
// One mapping of a target: its own window, the local address at its base, its routing windows
// (the parts no smaller mapping overlapping it takes; layout_mappings) and, for each of its aligned
// power-of-two blocks holding another mapping, the packet boundary that keeps packets in that block
// out of the mappings it holds.
struct MappingPlan {
  AddressWindow window;
  std::uint64_t local_base = 0;
  std::vector<AddressWindow> routes;
  std::vector<std::pair<AddressWindow, std::uint64_t>> boundaries;
  // Whether a sub-page routing window of the mapping shares its page with another target.
  bool shared = false;
  // Its mode condition and plane (mode_planes): window, routes and boundaries lie in that plane.
  std::map<std::string, bool> when;
  unsigned plane = 0;
  // The target flow it reaches (AddressMapping::flow).
  unsigned flow = 0;
  bool operator==(const MappingPlan &) const = default;
};
// One row of the address decode table, in base order: a routing window of one target mapping.
struct DecodeEntry {
  AddressWindow route;
  unsigned target = 0, mapping = 0;
  bool operator==(const DecodeEntry &) const = default;
};
// The (write, read) state bits a converter adds to its burst engine.
using StateBits = std::pair<unsigned, unsigned>;
// A pipe inside an endpoint, in the endpoint's domain: its storage resource
// (<source|target>/<name>/pipe/<place>), its mode and the payload bits of one slot.
struct EndpointPipe {
  std::string name;
  LinkPipeline mode;
  unsigned bits = 0;
  bool operator==(const EndpointPipe &) const = default;
};
struct SourcePlan {
  std::string name;
  // The network carries an APB initiator as an AXI4-Lite source; apb names its face. An AHB
  // initiator is an AXI4 source behind an AHB-Lite face (ahb), with no ID or length ports, and an
  // AXI3 initiator an AXI4 source behind AXI3 ports (axi3).
  Protocol protocol = Protocol::Axi4;
  std::optional<Protocol> apb;
  bool ahb = false, axi3 = false;
  unsigned apb_bits = 32, address_bits = 32, engine_bits = 32;
  // AxID and AxLEN port widths of a full AXI4 source.
  std::optional<unsigned> id_bits, len_bits;
  // pooled: reads and writes share one pool of `outstanding` transactions in flight (every
  // source, read-only ones too, except APB ones, which hold one transfer); each burst holds one
  // entry from its first packet to its final packet's last answer, narrow-split pieces one each.
  bool read_only = false, write_only = false, split_narrow = false, pooled = false;
  // Shared source burst capacity and, above one: the AXI IDs per direction each engine binds
  // to destinations (axi4_initiator.hpp), every ID it can have in flight; id_keys, the
  // {direction, AXI ID} keys the source can have in flight (at most `outstanding`), direction-only
  // for Lite; and id_table, its ID table entries (Initiator::id_table_entries). The request merger
  // keeps the table (rejoin_requests) only when it can fill, id_table below id_keys.
  unsigned outstanding = 1, outstanding_ids = 0, id_keys = 0, id_table = 0;
  // Scheduling levels, the rate limiter and the regulator.
  InitiatorQos qos{};
  std::optional<std::string> default_target; // Initiator::default_target
  // AxUSER port width and the driver of every network user flag, in flag order.
  unsigned user_bits = 0, response_user_bits = 0;
  std::vector<AttributeBit> user_flags;
  // The driver of every security flag, in name order.
  std::vector<AttributeBit> security_flags;
  // State bits of the width clamp of a source wider than a target it reaches, and of the packet
  // splitter of every AXI4 source, counted in its burst engine.
  std::optional<StateBits> clamp, packets;
  // Slots of its engine: a regulated source's packets stages hold requests that have left the
  // merger and have no context yet, as many as request_limit lets past it.
  unsigned engine_slots = 1;
  // Requests past the merger of such a source, when its pipes can reach that many; zero otherwise.
  unsigned request_limit = 0;
  // Its pipes in data flow order: aw, w, ar, packets/write, packets/read, issue.
  std::vector<EndpointPipe> pipes;
  // Initiator::route_classes of an initiator of several flows; empty with one.
  std::vector<std::string> flows;
  std::vector<IdClassInterval> id_ranges;
  std::string default_class;
  bool operator==(const SourcePlan &) const = default;
};
// One bank of a service target: where it sits in the target's window, and the output of the
// service switch it hangs on.
struct ServiceBank {
  RegisterBankPlan plan;
  std::uint64_t base = 0;
  unsigned branch = 0;
  bool operator==(const ServiceBank &) const = default;
};
struct ServicePlan {
  std::vector<ServiceBank> banks;
  // The clock domain of the banks and their loggers; empty: the target's.
  std::string domain{};
  bool operator==(const ServicePlan &) const = default;
};
// What a branch adds to the names of its crossings: nothing on a target of one branch.
inline std::string service_branch_suffix(const ServicePlan &service, unsigned branch) {
  return service.banks.back().branch == 0 ? std::string{} : "/branch" + std::to_string(branch);
}
struct TargetPlan {
  std::string name;
  // An AXI3 target is an AXI4 target behind AXI3 ports (axi3).
  Protocol protocol = Protocol::Axi4;
  bool axi3 = false;
  std::vector<MappingPlan> mappings; // in base order
  // data_bits is the endpoint engine width (a narrow APB target keeps the network width) and
  // apb_bits a narrow APB target's physical width.
  unsigned local_bits = 32, data_bits = 32, apb_bits = 32, hresp_bits = 1, outstanding = 1;
  // Target::flows: APB slave interfaces, or the flow numbers in an AXI4 target's AxID.
  unsigned flows = 1;
  // AxID and AxLEN port widths of an AXI4 target; planning sets the default ID width when absent.
  std::optional<unsigned> id_bits, len_bits;
  // The explicit read interleave granule in bytes (Target::read_interleave_bytes).
  std::optional<unsigned> read_interleave_bytes;
  bool read_only = false, write_only = false, back_to_back = false, fixed_single = false,
       complete_writes = false, fixed_length = false, read_side_effects = false;
  // Some source sends its decode misses here (default_target); planning sets it.
  bool fallback = false;
  std::optional<TargetAttributes> attributes;
  // AxUSER bit i: a constant or AttributeSource::User naming a network user flag by its index.
  std::vector<AttributeBit> user;
  unsigned response_user_bits = 0; // BUSER and RUSER port width
  std::optional<TargetTimeout> timeout;
  ExclusiveTarget exclusive{}; // mode None without support
  // The state bits of the narrow-burst merge of an upsized target, counted in its outlet engine.
  std::optional<StateBits> upsize;
  // Its pipes in data flow order: arrival, dispatch, aw, w, ar, response.
  std::vector<EndpointPipe> pipes;
  // A service target (InternalNetwork): the banks it serves in place of APB ports.
  std::optional<ServicePlan> service;
  // The routing windows of its mappings, mapping by mapping in base order; of one flow's mappings
  // only with a flow.
  std::vector<AddressWindow> routes(std::optional<unsigned> flow = std::nullopt) const {
    std::vector<AddressWindow> out;
    for (const auto &m : mappings)
      if (!flow || m.flow == *flow)
        std::ranges::copy(m.routes, std::back_inserter(out));
    return out;
  }
  // The bits of a flow number, zero for one flow.
  unsigned flow_bits() const { return static_cast<unsigned>(std::bit_width(flows - 1)); }
  bool operator==(const TargetPlan &) const = default;
};
// A connected (source, target) pair by index, with its packet limit in bytes (zero keeps the
// default) and the limit's configuration path.
struct PairPlan {
  unsigned source = 0, target = 0;
  std::uint64_t max_bytes = 0, boundary_bytes = 0;
  std::string limit_field{};
  bool operator==(const PairPlan &) const = default;
};
// The route of a source's decode misses: the default target, the window a miss moves into (first
// in base order of the miss mapping's target flow) and its flow pair (that target flow, the lowest
// reaching initiator flow). Miss mapping: the selected block's owner, or a named target's smallest.
struct MissRoute {
  unsigned target = 0;
  AddressWindow window;
  unsigned target_path = 0, source_path = 0;
  bool operator==(const MissRoute &) const = default;
};
struct TargetReach {
  bool reached = false;
  std::vector<std::uint64_t> flows;
  bool operator==(const TargetReach &) const = default;
};
// Compact endpoint-flow indices in name order. Transaction identity and observation records
// have separate layouts, each described from its lowest field upward.
struct TargetIdNumbering {
  unsigned initiator_bits = 0, target_bits = 0, range_bits = 0, sequence_bits = 0,
           direction_bits = 0;
  std::map<std::string, unsigned> initiators, targets;
  // The flows each numbered endpoint takes.
  std::map<std::string, unsigned> source_paths, target_paths;
  struct Field {
    std::string_view name;
    unsigned bits = 0, lsb = 0;
  };
  std::array<Field, 5> fields(bool record = false) const {
    std::array<Field, 5> out = record ? std::array<Field, 5>{{{"region_index", range_bits},
                                                              {"target_path", target_bits},
                                                              {"source_path", initiator_bits},
                                                              {"sequence", sequence_bits},
                                                              {"direction", direction_bits}}}
                                      : std::array<Field, 5>{{{"direction", direction_bits},
                                                              {"sequence", sequence_bits},
                                                              {"source_path", initiator_bits},
                                                              {"target_path", target_bits},
                                                              {"region_index", 0}}};
    unsigned at = 0;
    for (auto &f : out) {
      f.lsb = at;
      at += f.bits;
    }
    return out;
  }
  unsigned shift(std::string_view name, bool record = false) const {
    const auto layout = fields(record);
    const auto at = std::ranges::find(layout, name, &Field::name);
    if (at == layout.end())
      throw std::logic_error("Unknown route field");
    return at->lsb;
  }
  unsigned record_bits() const {
    const auto last = fields(true).back();
    return last.lsb + last.bits;
  }
  bool operator==(const TargetIdNumbering &) const = default;
};
// Derived once after route planning, indexed by source and target position. The implicit mapping
// and miss route of a source are values, so subsequent profile and obligation queries share them.
struct RouteIndex {
  std::vector<std::vector<TargetReach>> reaching;
  std::vector<std::optional<std::pair<unsigned, std::size_t>>> implicit_misses;
  std::vector<std::optional<MissRoute>> misses;
  TargetIdNumbering target_ids;
  bool operator==(const RouteIndex &) const = default;
};
// The names of records in order.
template <class Records> std::vector<std::string> names(const Records &records) {
  std::vector<std::string> out;
  std::ranges::transform(records, std::back_inserter(out), [](const auto &r) { return r.name; });
  return out;
}
// The word of a link without an explicit one, between end words from and to: the narrower end
// word that still carries the bandwidth of the slower end, at a frequency of 1 / period. With
// either period absent (one domain, or untimed domains) the ends count as equally fast, which
// takes the narrower word. Bandwidths are compared scaled by both periods.
inline unsigned edge_word(unsigned from, unsigned to, std::optional<std::uint64_t> from_ps,
                          std::optional<std::uint64_t> to_ps) {
  const std::uint64_t tf = from_ps && to_ps ? *from_ps : 1, tt = from_ps && to_ps ? *to_ps : 1;
  const std::uint64_t slower = std::min(from * tt, to * tf);
  const bool from_fits = from * tf >= slower, to_fits = to * tt >= slower;
  return from_fits && to_fits ? std::min(from, to) : from_fits ? from : to;
}
// One switch of one direction: its hardware identity (req_node<k>, rsp_node<k>), its configured
// name and its transport word.
struct NodePlan {
  std::string id, name;
  unsigned word_bytes = 0;
  bool operator==(const NodePlan &) const = default;
};
// One stage of a link in data flow order, clocked by domain: a register slice (Pipe), a 2:1 width
// adapter of the serialization ladder (width_adapter.hpp) or the link's clock adapter (the
// ClockCut of that name).
enum class StageKind : std::uint8_t { Pipe, Adapter, Crossing };
struct StagePlan {
  StageKind kind = StageKind::Adapter;
  // Binding stem: pipeline/<link>, adapter/<segment>/<hop> or cut/<link>. An adapter's segment is
  // the link, or <link>/link-in and <link>/link-out around an explicit link word; the receiver
  // side of a link without one is <link>/link-out too.
  std::string name, domain, segment;
  bool response = false, serializer = false; // serializer: wide -> narrow
  unsigned wide_bytes = 0, narrow_bytes = 0; // a pipe holds words of wide_bytes
  LinkPipeline pipe{};
  bool operator==(const StagePlan &) const = default;
};
// One link of one direction, "<req|rsp>/<from>><to>", between two placed objects (source/<name>,
// target/<name>, req/<node>, rsp/<node>): an endpoint attachment or a switch edge. Its stages are
// the sender pipe and adapters in the sender domain, the crossing, then the receiver adapters and
// pipe in the receiver domain (link_stages). A switch edge takes links[].sender_pipeline and
// links[].pipeline; an attachment takes its pipeline at the switch end.
struct LinkPlan {
  std::string name, from, to, from_domain, to_domain;
  bool response = false;
  unsigned from_bytes = 0, to_bytes = 0;
  // The explicit link word: adapt to it before the crossing and to the receiver after it.
  std::optional<unsigned> word;
  // The word without an explicit one (edge_word), placed the same way.
  unsigned edge_bytes = 0;
  LinkPipeline out_pipe{}, in_pipe{};
  std::vector<StagePlan> stages;
  unsigned crossing_bytes() const { return word.value_or(edge_bytes); }
  bool operator==(const LinkPlan &) const = default;
};
// The resolved arbiter of one physical output, shared by its read and write
// packets: candidates in hardware index order and the policy chosen for them.
struct ArbiterPlan {
  bool response = false;
  std::string node, output;
  std::vector<std::string> candidates;
  treadle::Spec spec;
  bool overridden = false; // set per output rather than by the default
  bool operator==(const ArbiterPlan &) const = default;
};
// Clock placement for an actual directed burst graph. Keys are scoped to
// source/<name>, target/<name>, req/<node>, rsp/<node>; request and response
// nodes with the same spelling remain distinct objects.
struct ClockPlacement {
  // Domain order names the clock_domains[i] diagnostics; crossing order names
  // the clock_crossings[i] diagnostics.
  std::vector<DomainPlan> domains;
  std::map<std::string, std::string> objects;
  std::vector<ClockCrossing> crossings;
  // Synchronizer stages of a crossing without its own; absent means 2.
  std::optional<unsigned> synchronizer_stages;
  std::vector<ClockRoot> roots;
  bool operator==(const ClockPlacement &) const = default;
};
struct AsyncClockCut {
  CrossingDepth depth{};
  unsigned count_bits = 0, index_bits = 0;
  unsigned synchronizer_stages = 0;
  bool reset_handshake = false;
  bool operator==(const AsyncClockCut &) const = default;
};
struct SyncClockCut {
  CrossingDepth depth{};
  unsigned count_bits = 0, index_bits = 0;
  bool fast = false;
  bool operator==(const SyncClockCut &) const = default;
};
struct EdgeAlignedCut {
  bool slow_pipeline = true;
  bool slow_from_sender = false;
  bool operator==(const EdgeAlignedCut &) const = default;
};
using ClockCutOptions = std::variant<AsyncClockCut, SyncClockCut, EdgeAlignedCut>;
// One explicitly classified adapter per cut. Only AsyncClockCut has Gray-count synchronizers.
struct ClockCut {
  std::string name, segment, from_domain, to_domain;
  std::vector<std::pair<std::string, unsigned>> fields;
  unsigned payload_bits = 0;
  ClockCutOptions options = AsyncClockCut{};
  bool operator==(const ClockCut &) const = default;
};
// A common policy for every physical switch output without an override.
struct ArbitrationDefaults {
  std::optional<treadle::Policy> policy = treadle::Policy::RoundRobin;
  treadle::Policy choose(unsigned) const { return policy.value_or(treadle::Policy::RoundRobin); }
  bool operator==(const ArbitrationDefaults &) const = default;
};
// One per-output override, keyed in NetworkPlan::output_arbitration by
// "<req|rsp>/<node>><output>" (output "node/<id>" or "endpoint/<name>"). priority lists candidate
// keys ("node/<id>", "endpoint/<name>") highest first; field is the configuration path for
// diagnostics.
struct OutputArbiterRequest {
  treadle::Policy policy = treadle::Policy::RoundRobin;
  std::vector<std::string> priority;
  std::string field;
  bool operator==(const OutputArbiterRequest &) const = default;
};
// How one source reaches an observed target: its route word's constant part, the sub-range of each
// aligned block of its windows in decode walk order, and its decode misses.
struct ObservedBlock {
  std::uint64_t base = 0, size = 0;
  unsigned range = 0;
  bool operator==(const ObservedBlock &) const = default;
};
struct ObservedSource {
  unsigned source = 0;
  std::uint64_t route = 0; // {initiator flow, target flow} above the sub-range
  std::vector<ObservedBlock> blocks;
  // Its route words hold its AxID and the direction below the sub-range.
  bool keeps_id = false;
  // A miss of this source here: its sub-range, and the address bits its record keeps.
  unsigned miss_range = 0;
  std::uint64_t miss_mask = 0;
  bool operator==(const ObservedSource &) const = default;
};
struct ObservedTarget {
  unsigned target = 0;
  std::vector<ObservedSource> sources;
  bool operator==(const ObservedTarget &) const = default;
};
// One first-error logger (error_logger.hpp), its bank on a service target.
struct FaultMonitorPlan {
  ErrorLoggerPlan logger;
  unsigned service = 0;
  unsigned bank = 0; // the first logger's in the target's banks; the chain's follow it
  std::vector<ObservedTarget> observed;
  // Sub-range field below the initiator and target fields of the route word, and the fields below.
  unsigned range_shift = 0;
  // A record keeps the whole address and every user flag (FaultMonitorRequest).
  bool keep_fields = false;
  bool operator==(const FaultMonitorPlan &) const = default;
};
// The network plan, built once from the validated definition (build_network_plan): the settings
// it reads from the definition, the admitted routes, adapter chains, the burst control bundle, the
// resource and wait obligations and, for an explicitly clocked graph, the clock placement.
struct NetworkPlan {
  std::string name;
  unsigned address_bits = 32;
  // DECERR for decode misses and target DECERR; false answers every failure SLVERR.
  bool error_codes = false;
  // transport.max_outstanding, id_table_entries and transport_word_bytes as written (map only).
  unsigned max_outstanding = 1;
  std::optional<unsigned> id_table_entries;
  unsigned transport_word_bytes = 0;
  // The map's topology record as JSON text; it has "generated" without an explicit topology.
  std::string topology_record;
  ArbitrationDefaults arbitration;
  std::map<std::string, OutputArbiterRequest> output_arbitration;
  // The domain of the same-domain composition; a clocked graph places every endpoint and
  // directional switch explicitly.
  DomainPlan domain{"main"};
  std::optional<ClockPlacement> placement;
  std::vector<ResetPort> reset_ports;
  std::vector<TestPort> test_ports;
  std::vector<EnablePort> enable_ports;
  std::vector<UngatePort> ungate_ports;
  // Address inputs and selectors in name order; mode planes take plane_bits transport address bits
  // from plane_shift (the global address width) up (mode_planes).
  std::vector<AddressInput> address_inputs;
  std::vector<AddressSelector> address_selectors;
  unsigned plane_shift = 0, plane_bits = 0;
  ClockGating clock_gating = ClockGating::None;
  ResetCoverage reset_coverage = ResetCoverage::All;
  TechnologyCells cells;
  // Until planned, exchange.request.graph and exchange.response.graph hold the switch graphs.
  ExchangePlan exchange;
  // Joint resource and wait obligations after alias merging. Switch objects carry no payload:
  // leaves alias the source descriptors, response leaves alias the reserved completions, and the
  // merges keep only owner state. Width adapters register as edge_pipeline resources (their
  // pipeline storage is inventoried) but add no wait edges: their waits coincide with the
  // message-level transfer wait of the segment they subdivide. A clocked plan replaces the
  // same-domain terminals with per-domain releases and routes the waits through the crossings.
  CrossGraph graph;
  // Sources and targets in name order, the connected pairs in (source, target) order and the
  // decode table.
  std::vector<SourcePlan> sources;
  std::vector<TargetPlan> targets;
  std::vector<FaultMonitorPlan> fault_monitors;
  // Some error logger may record a decode miss's whole address: the transport keeps it.
  bool observes_miss_address = false;
  std::vector<PairPlan> pairs;
  std::vector<DecodeEntry> decode;
  RouteIndex route_index;
  // Switches of both directions in identity order; the links of each direction (request first) in
  // graph order: entry attachments, switch edges, exit attachments.
  std::vector<NodePlan> nodes;
  std::vector<LinkPlan> links;
  unsigned source_bits = 0;
  // True when any switch node carries a transport word wider than the endpoint word; the flat
  // profile then switches channels to fragment layouts.
  bool laddered = false;
  unsigned burst_data_bits = 32, burst_id_bits = 0, burst_len_bits = 8; // ID, length: derived
  unsigned burst_source_data_bits = 32;
  // Some source or target data width differs from burst_data_bits: every endpoint attaches at its
  // own width.
  bool per_endpoint = false;
  // Some target data width differs from burst_data_bits: every reached AXI4 target merges narrow
  // bursts into target words (upsize_targets).
  bool per_target = false;
  // Weft response steering tag width (message.hpp point 12): enough bits to number the bursts in
  // flight of the largest source capacity; zero when every source keeps one.
  unsigned burst_tag_bits = 0;
  // Weft route path width (message.hpp point 14): with a switch input whose routes of one exit
  // leave by different outputs where the exit and entry alone cannot choose (a response split,
  // or a request split between the flows of one initiator), enough bits for the most flows of an
  // initiator and the most return paths into one; zero otherwise.
  unsigned burst_path_bits = 0;
  // Weft scheduling level width of the endpoint request faces (message.hpp point 15): three bits
  // when some source maps its AxQOS to levels (InitiatorQos::axqos_map), zero otherwise. The
  // transport carries it on every packet of a network with levels (transport_level_bits).
  unsigned burst_level_bits = 0;
  // Network user flags (message.hpp point 13), transport.user_flags in order; zero without them.
  unsigned burst_user_bits = 0, burst_response_user_bits = 0;
  // BurstProfile::record_bits, with an error logger whose lengths vary.
  unsigned burst_record_bits = 0;
  std::vector<std::string> user_flags;
  // BurstProfile::security_bits and the security flags in name order, the order a record keeps
  // them.
  unsigned burst_security_bits = 0;
  std::vector<std::string> security_flags;
  // The global address width of the endpoint faces.
  unsigned burst_address_bits = 32;
  // The transport layout (message.hpp point 16): the address bits a packet keeps between its
  // source and its target and the request route field.
  unsigned transport_address_bits = 32, target_bits = 0;
  // Transport fragment occupancy bits, from the widest endpoint word: six up to 32 bytes, seven for
  // 64 and eight for 128 (512/1024-bit attachments).
  unsigned occupancy_bits = 6;
  // Exclusive access: some target has network or target support. It adds the Weft exclusive fields
  // (message.hpp contract point 11) to every channel; false keeps the layout and bytes of the
  // default.
  bool exclusive = false;
  // Every physical switch output with two or more candidates, keyed "<req|rsp>/<node>><output>";
  // burst profile only.
  std::map<std::string, ArbiterPlan> arbiters;
  // The response transport carries the segment marker cut (message.hpp point 17): some target
  // interleaves read data (interleaved_target) and some response switch output arbitrates.
  bool response_segments = false;
  // Some response link widens: its assembler gathers runs (message.hpp point 18).
  bool response_runs = false;
  // The address bits of the widest word a response run gathers to; zero without runs.
  unsigned response_run_bits() const {
    unsigned widest = 0;
    for (const auto &link : links)
      if (link.response)
        for (const auto &stage : link.stages)
          if (stage.kind == StageKind::Adapter && !stage.serializer)
            widest = std::max(widest, stage.wide_bytes);
    return widest ? static_cast<unsigned>(std::countr_zero(widest)) : 0U;
  }
  // Clock placement (plan_clocking), by domain name; empty for the same-domain composition, whose
  // every other clock field below stays empty or zero.
  std::vector<DomainPlan> domains;
  // The domain of every placed object (source/<name>, target/<name>, req/<node>, rsp/<node>) and
  // physical resource.
  std::map<std::string, std::string> object_domains, resource_domains;
  std::vector<ClockCut> cuts;
  // fifo_payload_bits counts every slot and receive register; fifo_control_bits
  // counts indexes, counts, out_valid and count synchronizer stages.
  unsigned fifo_control_bits = 0, fifo_payload_bits = 0;
  // Explicit depths and synchronizer stages by directed (sender, receiver)
  // domain pair; the other pairs take synchronizer_stages.
  std::map<std::pair<std::string, std::string>, unsigned> explicit_depths, explicit_stages;
  unsigned synchronizer_stages = 0;
  // The reset handshake of every crossing: explicit per pair, else on with two or more reset
  // ports.
  std::map<std::pair<std::string, std::string>, bool> explicit_handshakes;
  bool reset_handshake = false;
  // The dependency graph of this plan and its check (dependency_graph.hpp).
  DependencyGraph dependency;
  DependencyReport dependency_report;
  bool clocked() const { return !domains.empty(); }
  const TargetPlan &target(std::string_view key) const {
    const auto it = std::ranges::lower_bound(targets, key, {}, &TargetPlan::name);
    if (it == targets.end() || it->name != key)
      throw std::out_of_range("No target " + std::string(key));
    return *it;
  }
  // The packet group of a target flow: the flows of the targets before it, then its flow (the
  // target's index without target flows).
  unsigned flow_group(unsigned index, unsigned flow) const {
    return std::accumulate(targets.begin(), targets.begin() + index, flow,
                           [](unsigned sum, const TargetPlan &t) { return sum + t.flows; });
  }
  unsigned target_index(std::string_view key) const {
    return static_cast<unsigned>(&target(key) - targets.data());
  }
  const NodePlan &node(std::string_view id) const {
    const auto it = std::ranges::lower_bound(nodes, id, {}, &NodePlan::id);
    if (it == nodes.end() || it->id != id)
      throw std::out_of_range("No switch " + std::string(id));
    return *it;
  }
  // A key naming a switch identity ("node/<id>") by its configured name; other keys unchanged.
  std::string label(const std::string &key) const {
    const auto it =
        std::ranges::find(nodes, key.substr(std::min<std::size_t>(5, key.size())), &NodePlan::id);
    return key.starts_with("node/") && it != nodes.end() ? "node/" + it->name : key;
  }
  // Every width adapter stage in link order.
  std::vector<StagePlan> adapters() const {
    std::vector<StagePlan> out;
    for (const auto &link : links)
      std::ranges::copy_if(link.stages, std::back_inserter(out),
                           [](const StagePlan &s) { return s.kind == StageKind::Adapter; });
    return out;
  }
  // Whether a source reaches a mode-dependent mapping: its engine then holds a mode plane per
  // direction.
  bool decodes_modes(unsigned source) const {
    return std::ranges::any_of(pairs, [&](const PairPlan &x) {
      return x.source == source &&
             std::ranges::any_of(targets[x.target].mappings,
                                 [](const MappingPlan &m) { return m.plane; });
    });
  }
  // Some source sets a scheduling level, fixed or by its regulator.
  bool levels() const {
    return std::ranges::any_of(sources, [](const SourcePlan &s) {
      return s.qos.levels.write || s.qos.levels.read || s.qos.picks() || !s.qos.axqos_map.empty();
    });
  }
  // Some source maps its AxQOS to levels: request packets then carry a level field.
  bool mapped() const {
    return std::ranges::any_of(sources,
                               [](const SourcePlan &s) { return !s.qos.axqos_map.empty(); });
  }
  // The scheduling levels a packet may take: 0 to the highest configured level.
  unsigned level_count() const {
    unsigned top = 0;
    for (const auto &s : sources) {
      top = std::max({top, s.qos.levels.write, s.qos.levels.read});
      if (!s.qos.axqos_map.empty())
        top = std::max(top, s.qos.axqos_map.back());
      if (const auto &g = s.qos.scheduler)
        top = std::max({top, g->under_level, g->over_level});
    }
    return top + 1;
  }
  // The transport level field width (message.hpp point 15): three bits with scheduling levels.
  unsigned transport_level_bits() const { return levels() ? 3 : 0; }
  // The width of the pressure sideband (thermometer: bit k is set while some packet it covers is
  // above level k), zero without levels above zero.
  unsigned level_hint_bits() const { return levels() ? level_count() - 1 : 0; }
  // Some source has a regulator.
  bool regulated() const {
    return std::ranges::any_of(sources, [](const SourcePlan &s) { return s.qos.picks(); });
  }
  // The routing windows of every target in target order.
  std::vector<AddressWindow> windows() const {
    std::vector<AddressWindow> out;
    for (const auto &t : targets)
      std::ranges::copy(t.routes(), std::back_inserter(out));
    return out;
  }
  unsigned stages(const std::string &sender, const std::string &receiver) const {
    const auto it = explicit_stages.find({sender, receiver});
    return it == explicit_stages.end() ? synchronizer_stages : it->second;
  }
  // The domain of an error log's logger; only a clocked network asks.
  const std::string &observer_domain(const std::string &observer_name) const {
    const auto &observer = *std::ranges::find_if(
        fault_monitors, [&](const auto &o) { return o.logger.name == observer_name; });
    const auto &served = targets[observer.service];
    return served.service->domain.empty() ? object_domains.at("target/" + served.name)
                                          : served.service->domain;
  }
  bool handshake(const std::string &sender, const std::string &receiver) const {
    const auto it = explicit_handshakes.find({sender, receiver});
    return it == explicit_handshakes.end() ? reset_handshake : it->second;
  }
  bool operator==(const NetworkPlan &) const = default;
};
// The route class of a source AxID in its total interval partition.
unsigned id_flow(const SourcePlan &source, std::uint64_t id);
// Definition queries shared by the definition checks (validation.cpp) and build_network_plan.
bool connects(const Definition &d, const std::string &source, const std::string &target);
// Whether it connects one flow pair of them (ConnectivityEntry).
bool connects(const Definition &d, const std::string &source, unsigned source_path,
              const std::string &target, unsigned target_path);
// The flow pairs (initiator, target, initiator flow, target flow) of connected pairs that the
// matrix leaves out.
std::set<std::tuple<std::string, std::string, unsigned, unsigned>>
unconnected_flows(const Definition &d);
bool apb_initiator(const Initiator &initiator);
// A full AXI burst port: AXI4, or AXI3, which the network carries as AXI4 (absent counts as AXI4).
bool full_axi(std::optional<Protocol> protocol);
unsigned source_engine_width(const Initiator &initiator, unsigned fallback);
// The network data width (transport.data_width, else the narrowest endpoint engine width, at most
// 64 bits) and the source width default.
unsigned network_data_width(const Definition &d);
unsigned source_data_width(const Definition &d, unsigned network_bits);
// The {source, AxID} keys of read-write sources that can reach a target: zero when no exclusive
// access can arrive.
std::uint64_t exclusive_keys(const Definition &d, const Target &t);
// The hardware identity of each switch of one direction, by configured name: req_node<k> or
// rsp_node<k> in name order, skipping endpoint names.
std::map<std::string, std::string> switch_identities(const Definition &d,
                                                     const ExchangeGraph &graph, bool response);
// Whether the definition is an explicitly clocked graph.
bool clocked_graph(const Definition &d);
// The mappings of every target cut into aligned power-of-two blocks, where blocks overlap the
// smaller one decoding (definition order of blocks: size, then base). Per target and mapping the
// routing windows its blocks keep (merged, then whole-page runs and power-of-two sub-pages) and,
// per block holding another mapping, its packet boundary: the smallest of the block size and of
// the alignments of the mappings directly inside it, except one starting at the block base or at
// another's end. overlaps lists (outer, inner, same size) block pairs.
struct MappingLayout {
  std::vector<std::vector<
      std::pair<std::vector<AddressWindow>, std::vector<std::pair<AddressWindow, std::uint64_t>>>>>
      mappings;
  std::set<std::tuple<std::pair<unsigned, unsigned>, std::pair<unsigned, unsigned>, bool>> overlaps;
};
MappingLayout layout_mappings(const Definition &d);
// Mode planes (docs/address-map.md#address-selectors): each distinct mapping condition, in
// condition order, takes plane 1, 2, ... of the transport address space, at address bits
// address_width and up; mappings without one stay in plane 0. Mappings of different planes never
// overlap there. A window as naturally aligned power-of-two blocks, lowest first.
std::vector<AddressWindow> aligned_blocks(const AddressWindow &window);
std::vector<std::map<std::string, bool>> mode_planes(const Definition &d);
unsigned plane_bits(const Definition &d);
unsigned mapping_plane(const Definition &d, const AddressMapping &m);
// Plans the network of a definition that passed the definition checks (network_plan.cpp): the
// switch graphs and their arbitration, the composition and clock planning stages, then the
// capability rules (capability.hpp) and the obligation self-check.
// What a network plan takes beside the definition and users cannot ask for yet.
struct FaultMonitorRequest {
  std::string name;
  std::string service; // the service target whose bank holds the logger
  std::vector<std::string> targets;
  std::vector<unsigned> ignored_codes;
  // Records keep a packet's whole transport address and every user flag.
  bool keep_fields = false;
  // The values a record's flags must have for it to enter the logger; a flag not named may have
  // any.
  std::map<std::string, bool> user_filter{}, security_filter{};
  // Where the logger's bank sits on the service target; the first error log without a place takes
  // the route the target was given.
  std::optional<unsigned> route{};
  std::uint64_t base = 0;
  // The record leaves to the next logger though this one logs it.
  bool forward = false;
  // The loggers after this one in the error log's chain.
  struct Chained {
    std::vector<unsigned> ignored_codes;
    std::map<std::string, bool> user_filter, security_filter;
    unsigned route = 0;
    std::uint64_t base = 0;
    bool forward = false;
  };
  std::vector<Chained> chain{};
  // The clock domain of the logger and its bank; empty: the service target's.
  std::string clock_domain = {};
  unsigned serial_word_bits = 32;
};
// Security flags apart from the user flags: each request carries them from its source to its
// target, where only an error logger reads them. They grant and deny nothing.
struct SecurityFlagsRequest {
  std::vector<std::string> flags;
  // Per source, the driver of each flag.
  std::map<std::string, std::map<std::string, AttributeBit>> drivers;
};
struct InternalNetwork {
  // Targets by name that serve a register bank instead of their APB4 ports.
  std::map<std::string, RegisterBankPlan> service_targets;
  std::vector<FaultMonitorRequest> fault_monitors;
  SecurityFlagsRequest security;
};
Result<NetworkPlan> build_network_plan(const Definition &definition,
                                       const InternalNetwork &internal = {});
} // namespace nocloom::detail
