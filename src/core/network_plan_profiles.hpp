#pragma once
#include "ahb_outlet.hpp"
#include "axi4_outlet.hpp"
#include "axi4_packetizer.hpp"
#include "axi4_upsize.hpp"
#include "axi4_width_clamp.hpp"
#include "exchange_composition.hpp"
#include "service_outlet.hpp"
#include <span>

namespace nocloom::detail {
inline std::string flow_id(const std::string &source, const std::string &operation) {
  return "source/" + source + "/" + operation;
}
bool reaches(const NetworkPlan &plan, const std::string &source, const std::string &target);
std::optional<unsigned> default_target(const NetworkPlan &plan, const std::string &source);
// Reached mappings with global bases and target indices in name order.
std::optional<DecodeEntry> select_default_mapping(std::span<const DecodeEntry> mappings,
                                                  unsigned address_bits);
// The flows of a source (bit k for flow k) whose request routes reach one flow of a target.
std::uint64_t reaching_flows(const NetworkPlan &plan, const std::string &source,
                             const std::string &target, unsigned target_path);
// Every flow of a source.
std::uint64_t all_flows(const SourcePlan &source);
void build_route_index(NetworkPlan &plan);
std::optional<MissRoute> miss_route(const NetworkPlan &plan, const std::string &source);
std::vector<unsigned> reaching_sources(const NetworkPlan &plan, const std::string &target);
unsigned least_target_id_bits(const TargetPlan &target);
std::vector<ExclusiveWindow> exclusive_windows(const NetworkPlan &plan, const std::string &source);
bool ahb_target(const NetworkPlan &plan, const std::string &target);
bool apb_target(const NetworkPlan &plan, const std::string &target);
bool lite_target(const NetworkPlan &plan, const std::string &target);
bool dedicated_protocol(Protocol protocol);
// Whether this source's clamp gathers each read apart (Axi4WidthClampProfile::reassembly).
bool clamp_reassembles(const NetworkPlan &plan, unsigned source);
// By target and by source: interleaved_target and clamp_reassembles, once for all.
std::vector<bool> interleaved_targets(const NetworkPlan &plan);
std::vector<bool> reassembling_sources(const NetworkPlan &plan,
                                       const std::vector<bool> &interleaved);
Axi4WidthClampProfile clamp_profile(const NetworkPlan &plan, unsigned source,
                                    bool fixed_reject = false);
// Packet bytes, limit and boundary of a target or a (source, target) pair.
struct PacketLimits {
  std::uint64_t bytes = 4, max_bytes = 128, boundary_bytes = 0;
  std::string error;
};
PacketLimits pair_limits(const NetworkPlan &plan, const PairPlan &pair,
                         std::optional<std::size_t> mapping = std::nullopt,
                         std::uint64_t block = 0);
unsigned network_len_bits(const NetworkPlan &plan);
// Whether an AXI4 target aligns the WRAP reads of an AXI4 source (Axi4PacketWindow::align_wrap):
// both as wide as the target's word, with no width conversion between, no dispatch pipe at the
// target, no read side effects and an AXI4 (not AXI3) face at each end.
bool wrap_aligned(const NetworkPlan &plan, unsigned source, unsigned target);
// Whether an AXI4 target aligns the WRAP reads of any source reaching it.
bool aligns_wrap_reads(const NetworkPlan &plan, unsigned target);
Axi4PacketizerProfile packet_profile(const NetworkPlan &plan, unsigned source);
// The source reaches a target with read side effects (its splitter sends exact packets).
bool exact_packets(const NetworkPlan &plan, unsigned source);
Axi4UpsizeProfile upsize_profile(const NetworkPlan &plan, unsigned target);
std::set<std::string> upsize_targets(const NetworkPlan &plan);
// An AXI4 or AXI3 target whose read data may interleave IDs (Axi4OutletProfile::interleave): one
// without a target merge that serves multi-beat reads, with several flows, a nonzero ID width or
// writes. A single-flow, zero-ID, read-only target keeps the non-interleaved outlet.
bool interleaved_target(const NetworkPlan &plan, const TargetPlan &target);
// The target words a read of the target keeps together: one, or the explicit multi-word granule.
unsigned interleave_words(const TargetPlan &target);
// The source reaches a target with a read interleave size above one word.
bool reads_granule_target(const NetworkPlan &plan, unsigned index);
// A target of read interleave size 0: its response joins without a stage or segments.
bool joins_directly(const TargetPlan &target);
// A decode packet is marked with the error flag (decode_packet.hpp): some target's result waits on
// readiness, or an error logger reads each word's result.
bool decode_flag(const NetworkPlan &plan);
// An outlet that joins its responses itself: an interleaved or a direct one.
bool joined_target(const NetworkPlan &plan, const TargetPlan &target);
unsigned whole_target_id_bits(const NetworkPlan &plan, const TargetPlan &target);
// The component profiles of one source or target, by index.
Axi4InitiatorProfile initiator_profile(const NetworkPlan &plan, unsigned source);
// The block holding every global address that reaches a target's outlet, and the address bits
// its registers keep.
AddressBlock arrival_block(const NetworkPlan &plan, const TargetPlan &target);
unsigned arrival_bits(const NetworkPlan &plan, const TargetPlan &target);
// The transport span of a target (message.hpp point 16): the aligned block of its routing windows
// widened to whole pages, with the page above them when it answers some source's misses; none
// when that reaches the top of the global address space.
AddressBlock transport_span(const NetworkPlan &plan, const TargetPlan &target);
// The burst layouts of a plan: the endpoint faces' and the transport's.
BurstProfile endpoint_burst(const NetworkPlan &plan);
BurstProfile transport_burst(const NetworkPlan &plan);
BurstOutletProfile outlet_profile(const NetworkPlan &plan, unsigned target);
Axi4OutletProfile axi4_outlet_profile(const NetworkPlan &plan, unsigned target);
ServiceOutletProfile service_outlet_profile(const NetworkPlan &plan, unsigned target);
unsigned record_bytes_minus_one_bits(const NetworkPlan &plan);
Result<FaultMonitorPlan> plan_fault_monitor(const NetworkPlan &plan,
                                            const FaultMonitorRequest &request);
} // namespace nocloom::detail
