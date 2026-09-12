#pragma once
#include "axi4_initiator.hpp"
#include "component.hpp"
#include "network_plan.hpp"
#include "nocloom/generation.hpp"
#include <algorithm>
#include <set>

namespace nocloom::detail {
struct ExchangeHardware {
  HardwareIR ir;
  NetworkPlan plan;
  std::map<std::string, ValueId> bindings;
  std::vector<CrossStorageBinding> storage;
};
// Transport word bytes at which an endpoint attaches to the exchange.
unsigned endpoint_word(const NetworkPlan &plan, const std::string &endpoint);
// The composition stage of build_network_plan: admitted routes, adapter chains with the burst
// control bundle and the burst resource/wait obligations.
Status plan_composition(NetworkPlan &plan);
// The clock placement stage of build_network_plan (exchange_clocking.cpp): segments, crossing
// FIFOs and the owning domain of every object, adapter and resource; rewrites the obligations
// through the crossings. Planning only: it does not claim CDC or physical closure.
void plan_clocking(NetworkPlan &plan);
// The obligations of a placed plan rebuilt: the same-domain ones in their owning domains, one
// release per domain and every admitted route through its crossing FIFOs.
CrossGraph clocked_obligations(const NetworkPlan &plan);
// The resource and wait obligations of a plan (plan_obligations.cpp), and their check: the
// derived budgets against the components' own state, the graph against a fresh build.
CrossGraph build_axi4_exchange_obligations(const NetworkPlan &plan);
Status verify_axi4_exchange_obligations(const NetworkPlan &plan, bool clocked = false);
// The full AXI4 burst exchange composition behind the schema 10 network. AXI4, AXI4-Lite and APB
// initiator endpoints and the AXI4, AXI4-Lite, APB and AHB target outlets join the request and
// response switch graphs, carrying Weft burst packets (message.hpp contract v1) over fragment
// channels whose grants occupy per packet. Responses carry the (source, id) sideband of the burst
// contract; routing back to a source uses the source identity.
//
// Capacity: each source defaults to one burst, with reads and writes sharing its capacity of
// up to 64 bursts. The bursts of one AXI ID go to one destination at a time while
// different IDs may go to different targets (per-ID ordering, responses steered by the Weft tag, no
// reorder or interleave storage). AXI4 and AXI3 targets share up to 16 packet slots across reads
// and writes, matching R and B by target ID (behind a width merge reads share one
// {source, AxID}). AXI4-Lite targets keep up to 256 packets per direction. Bounded packets use
// independent target slots; the source packetizer retains original-burst boundaries and limits
// split bursts to four packets in flight. Per-target merges retain K packet contexts per direction;
// B restores the oldest matching key and R holds its FIFO head through the last upstream beat.
// A multi-burst source decodes at most 256 routing windows (validation.cpp). Clocked graphs carry
// the response tag through every crossing FIFO, the per-target width merge in front of an AXI4
// outlet through its write stage and read descriptor.
//
// Routing premise: admitted bursts never cross a 4 KiB block (ingress admission). Page routes keep
// one target service per burst.
//
// Composes the endpoints, packet-occupying switches, window shims and adapter chains of a plan
// into one composition, same-domain or clocked by its placement. With synchronous register reset
// every gate also opens while its domain reset is low.
Result<ExchangeHardware>
lower_axi4_exchange_composition(const NetworkPlan &plan,
                                RegisterReset register_reset = RegisterReset::Asynchronous);
// Structural and resource-obligation checks over the burst composition, including the response-face
// lifecycle anchor behind the return reservation.
Status verify_axi4_exchange_composition(const ExchangeHardware &hardware);
// Structural self-check of the multiple-outstanding sources (run by the lowering): each queued
// source's
// decode, evaluated on its descriptor registers, equals the target the request switches' own
// selection logic picks for that start address; admission failures (exclusive, 4 KiB crossing,
// illegal WRAP) decode to local completion.
Status verify_network_decode(const ExchangeHardware &hardware);
// The clocked composition against its placement: resources, crossing FIFOs, synchronizers, resets
// and the domain of every register input.
Status verify_exchange_clock_hardware(const ExchangeHardware &hardware);
} // namespace nocloom::detail
