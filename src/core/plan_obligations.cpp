#include "axi4_outlet.hpp"
#include "diagnostic.hpp"
#include "network_plan_profiles.hpp"
#include "width_adapter.hpp"
#include <algorithm>
#include <bit>
#include <numeric>
#include <tuple>

namespace nocloom::detail {
namespace {
constexpr unsigned endpoint_word_bytes = 4;
// Payload register bits of one derived adapter instance: the deserializer stores one narrow
// fragment (control bundle, occupancy and lanes; a request run's end is the completing run's, not
// stored); the serializer keeps only its occupation bit and stores no payload. The burst control
// bundle is field-derived (it carries the descriptor).
unsigned adapter_payload_bits(const StagePlan &adapter, unsigned source_bits,
                              const BurstProfile &burst) {
  if (adapter.serializer)
    return 0;
  const unsigned lanes = adapter.narrow_bytes / endpoint_word_bytes;
  // A response assembler stores its words below the top, the run's start and what accumulates.
  if (adapter.response)
    return (adapter.wide_bytes - adapter.narrow_bytes) / endpoint_word_bytes * 32 +
           static_cast<unsigned>(std::countr_zero(adapter.wide_bytes)) + 2 +
           (burst.exclusive ? 1 : 0);
  unsigned control = 0;
  for (const auto &[name, width] :
       fragment_fields(adapter.response, adapter.narrow_bytes, source_bits, burst)) {
    if (name == "error" || name == "first" || name == "last" || name == "cut" || name == "extent" ||
        name == "ends" || name == "wrap_end" || name.starts_with("data") || name.starts_with("be"))
      continue;
    control += width;
  }
  return control + lanes * (adapter.response ? 32 : 36);
}
// Payload register bits of the burst endpoint engine storage records, computed from the profile.
// The lowering registers the actual state words and the verifier compares, so a drift between this
// budget and the components is a lowering-time failure, never silent.
// The one AxSIZE of a Lite (or APB) source's face, as axi4_initiator.cpp sets it; 0 otherwise.
unsigned lite_beat_size(const SourcePlan &sp) {
  if (sp.protocol != Protocol::Axi4Lite)
    return 0;
  return sp.apb ? static_cast<unsigned>(std::countr_zero(sp.apb_bits / 8))
                : (sp.engine_bits == 64 ? 3U : 2U);
}
struct Axi4BurstStateBits {
  unsigned source_engine[2];
  unsigned source_return[2];
};
Axi4BurstStateBits axi4_burst_state_bits(unsigned id_bits, unsigned len_bits, unsigned address_bits,
                                         unsigned source_data_bits, unsigned outstanding = 1,
                                         bool exclusive = false, bool lite = false,
                                         unsigned user_bits = 0, unsigned response_user_bits = 0,
                                         unsigned level_bits = 0, unsigned beat_size = 0) {
  const unsigned source_be = source_data_bits / 8;
  // Captured descriptor: addr, len, size, burst, lock, cache, prot[, id][, user][, level].
  const unsigned descriptor = address_bits + 13 + len_bits + id_bits + user_bits + level_bits;
  Axi4BurstStateBits bits{};
  // + beat address (without the bits below a Lite engine's one beat size); a Lite engine also
  // buffers the beat (data and strobes).
  bits.source_engine[0] =
      descriptor + address_bits - beat_size + (lite ? source_data_bits + source_be : 0);
  bits.source_engine[1] = descriptor;
  // Aggregated B response register; R skid: data, result, error, last, and exclusive_okay with
  // the exclusive fields; each with the response user field.
  bits.source_return[0] = 2 + response_user_bits;
  bits.source_return[1] = source_data_bits + 4 + (exclusive ? 1 : 0) + response_user_bits;
  if (outstanding > 1) {
    // The slot table: per slot the WLAST poison (write).
    bits.source_engine[0] += outstanding;
  }
  return bits;
}
// The write or read half of converter state bits.
unsigned half(const std::optional<StateBits> &bits, unsigned direction) {
  return !bits ? 0 : direction == 0 ? bits->first : bits->second;
}
// Payload bits of an AXI4 outlet engine, counted by the outlet's own tables; upsize merge state
// counts with its outlet engine: with the merge in front, one burst's lifecycle spans both.
unsigned outlet_engine_bits(const NetworkPlan &plan, const std::string &target,
                            unsigned direction) {
  Axi4OutletProfile outlet;
  outlet.data_bits = endpoint_word(plan, target) * 8;
  outlet.id_bits = plan.burst_id_bits;
  outlet.len_bits = plan.burst_len_bits;
  outlet.source_bits = plan.source_bits;
  outlet.start_address = true;
  outlet.transport_address_bits = plan.burst_address_bits;
  outlet.arrival = arrival_block(plan, plan.target(target));
  outlet.max_outstanding = plan.target(target).outstanding;
  outlet.exclusive_target = plan.target(target).exclusive;
  outlet.merged = plan.target(target).upsize.has_value();
  outlet.user_bits = plan.burst_user_bits;
  outlet.user = plan.target(target).user;
  outlet.response_user_bits = plan.burst_response_user_bits;
  outlet.response_user_port_bits = plan.target(target).response_user_bits;
  outlet.axi3 = plan.target(target).axi3;
  outlet.interleave = joined_target(plan, plan.target(target));
  outlet.interleave_beats = interleave_words(plan.target(target));
  outlet.response_direct = joins_directly(plan.target(target));
  outlet.merger_hints =
      outlet.max_outstanding > 1 && !plan.target(target).write_only && !outlet.interleave;
  outlet.read_only = plan.target(target).read_only;
  outlet.write_only = plan.target(target).write_only;
  outlet.error_codes = plan.error_codes;
  outlet.decode_flag = decode_flag(plan);
  outlet.exclusive = plan.exclusive;
  outlet.port_id_bits = plan.target(target).id_bits;
  outlet.align_wrap =
      aligns_wrap_reads(plan, static_cast<unsigned>(&plan.target(target) - plan.targets.data()));
  return half(axi4_outlet_outstanding_bits(outlet), direction) +
         half(plan.target(target).upsize, direction);
}
// Data and byte-lane bits of a dedicated target word beyond 32 bits.
unsigned dedicated_wide_bits(const NetworkPlan &plan, const std::string &target) {
  const unsigned bytes = endpoint_word(plan, target);
  return bytes > 4 ? 9 * (bytes - 4) : 0U;
}
unsigned dedicated_engine_bits(const NetworkPlan &plan, const std::string &target,
                               unsigned direction) {
  const auto &t = plan.target(target);
  const bool monitored = t.exclusive.mode == ExclusiveAccess::Network;
  // The pipelined AXI4-Lite engine: a one-entry response stage and one packet queue per
  // direction; a write packet's slot keeps its local answer, and the head packet its B error.
  if (t.protocol == Protocol::Axi4Lite) {
    const unsigned capacity = t.outstanding;
    const unsigned word = endpoint_word(plan, target);
    const unsigned bits = arrival_bits(plan, t), len = plan.burst_len_bits;
    const unsigned ids = plan.burst_id_bits, srcs = plan.source_bits;
    // The response user field of a target with BUSER and RUSER: in the response stage, and the
    // first B of the head write packet or the head of a read packet.
    const unsigned user = t.response_user_bits ? plan.burst_response_user_bits : 0U;
    // The exclusive monitor keeps a write packet's length and burst.
    const unsigned range = monitored ? len + 2 : 0U;
    if (direction == 0)
      return capacity * (bits + 5 + range + ids + srcs) + (plan.error_codes ? 2 : 1) + bits + 5 +
             ids + srcs + range + 2 * user;
    return capacity * (bits + len + 13 + ids + srcs + (t.user.empty() ? 0 : plan.burst_user_bits)) +
           2 * bits + bits + 5 + word * 8 + ids + srcs + 2 * user;
  }
  // A pipelined AHB read keeps subread data and result, its issue address apart from
  // the response beat, and a two-entry response buffer of data and result. A pipelined AHB write
  // keeps one word (address, data, byte lanes) besides HWDATA. A merge in front of a wide AHB
  // target counts with its engine. Flags and start offsets (r.transfer, r.start_offset, w.peeled,
  // *.start) are control state, not payload.
  if (direction == 1 && t.protocol == Protocol::AhbV3)
    return 28U + 4 * arrival_bits(plan, t) + plan.burst_len_bits + 3 * plan.burst_id_bits +
           3 * plan.source_bits + 24 * endpoint_word(plan, target) + half(t.upsize, direction) +
           (t.ahb5 ? plan.burst_user_bits : 0);
  if (t.protocol == Protocol::AhbV3)
    return 49U + 5 * arrival_bits(plan, t) + 4 * plan.burst_len_bits + 4 * plan.burst_id_bits +
           4 * plan.source_bits + 9 * endpoint_word(plan, target) + half(t.upsize, direction) +
           (t.ahb5 ? 3 * plan.burst_user_bits : 0);
  // APB: the write side stages each completed packet's B snapshot in two
  // slots (address, result, size, [id,] [source]); the read side keeps two
  // full entries (address, result, data, size, [id,] [source]). A serial
  // outlet without a timer keeps one slot and one entry. Addresses keep the
  // bits of the target's arrival block. A full-width APB3/APB2 word's lanes are one bit.
  // A service target's payload is its gathered read data; a word waits on its face.
  if (t.service)
    return direction == 1 ? 24U : 0U;
  if (apb_target(plan, target)) {
    const unsigned bits = arrival_bits(plan, t), len = plan.burst_len_bits;
    const unsigned ids = plan.burst_id_bits, srcs = plan.source_bits;
    const auto form =
        apb_outlet_form(outlet_profile(plan, static_cast<unsigned>(&t - plan.targets.data())));
    const unsigned user =
        (t.user.empty() ? 0U : plan.burst_user_bits) +
        (t.response_user_bits ? plan.burst_response_user_bits * (form.single ? 2U : 3U) : 0U);
    const bool one_lane =
        (t.protocol == Protocol::Apb3 || t.protocol == Protocol::Apb2) && t.apb_bits >= 32;
    if (direction == 0) {
      // A word issued as it is taken: its data and running result, the response entry's fields.
      if (form.write_in_entry)
        return user + bits + 37 + ids + srcs;
      const unsigned entry = bits + 5 + ids + srcs + (monitored ? len + 2 : 0U);
      if (form.at_once)
        return user + bits + len + 46 + ids + srcs + entry;
      return user + 4 * bits + len + 60 + 3 * ids + 3 * srcs + (monitored ? 2 * (len + 2) : 0U) -
             (form.single ? entry : 0U) - (one_lane ? 3U : 0U);
    }
    if (!form.single)
      return user + 4 * bits + len + 94 + 3 * ids + 3 * srcs + (t.apb_bits < 32 ? 32U : 0U);
    // Its address, data, code and length, the response entry's fields; narrow words keep their
    // mask and gather, a sub-page window also the attributes and the word's code.
    return user + 2 * bits + len + 41 + ids + srcs + (form.at_once ? 0U : 4U) +
           (form.packet_coded ? 0U : 9U) + (t.apb_bits < 32 ? 32U : 0U);
  }
  return 0U; // unreachable: AHB, APB and AXI4-Lite return above
}
unsigned dedicated_bus_bits(const NetworkPlan &plan, const std::string &target) {
  // Latched local address, mask, data and protection; APB3/APB2 latch no
  // protection, and a serial APB outlet without a timer drives its word buffer. AHB latches the
  // address phase (address, HSIZE and a four-bit HPROT, no mask) and the data phase's HWDATA. A
  // target of several flows latches the transfer's flow.
  const auto &t = plan.target(target);
  if (t.service)
    return 0U;
  if (t.protocol == Protocol::AhbV3)
    return 7U + t.local_bits + 8 * endpoint_word(plan, target) + t.flow_bits() +
           (t.ahb5 ? t.hprot_bits - 4 + unsigned(t.hnonsec) + t.hmaster_bits : 0);
  const unsigned fixed = t.protocol == Protocol::Apb3 || t.protocol == Protocol::Apb2 ? 36U : 39U;
  const bool single = apb_target(plan, target) && !t.back_to_back && !t.timeout;
  const auto address = apb_target(plan, target)
                           ? apb_bus_address_bits(outlet_profile(
                                 plan, static_cast<unsigned>(&t - plan.targets.data())))
                           : t.local_bits;
  return fixed + address + dedicated_wide_bits(plan, target) - (single ? 32U : 0U) +
         static_cast<unsigned>(t.user.size()) + t.flow_bits();
}
// The obligations of one plan in the single domain, built source flows first, then target
// services, packet grants and link stages.
struct Obligations {
  const NetworkPlan &plan;
  CrossGraph graph{};
  const std::string init = "domain/" + plan.domain.name + "/init";
  void resource(std::string id, std::string role, unsigned bits, std::string owner,
                std::string release) {
    graph.resources.push_back(
        {id, id, plan.domain.name, std::move(role), std::move(owner), std::move(release), bits});
  }
  void wait(std::string from, std::string to, CrossWaitKind kind, std::string condition) {
    graph.waits.push_back({std::move(from), std::move(to), std::move(condition), kind});
  }
  void sources() {
    for (const auto &sp : plan.sources) {
      const auto &source = sp.name;
      auto source_state = axi4_burst_state_bits(
          plan.burst_id_bits, plan.burst_len_bits, plan.burst_address_bits, sp.engine_bits,
          sp.engine_slots, plan.exclusive, sp.protocol == Protocol::Axi4Lite,
          plan.burst_user_bits + plan.burst_security_bits, plan.burst_response_user_bits,
          sp.qos.axqos_map.empty() ? 0U : plan.burst_level_bits, lite_beat_size(sp));
      if (sp.engine_slots > 1 && sp.protocol != Protocol::Axi4Lite && !sp.apb && !sp.ahb)
        source_state.source_engine[1] += sp.engine_slots * std::countr_zero(sp.engine_bits / 8);
      // An APB face holds its transfer: no descriptor, beat or count in the engine.
      if (sp.apb)
        source_state.source_engine[0] = source_state.source_engine[1] = 0;
      for (unsigned direction = 0; direction < 2; ++direction) {
        const auto operation = direction == 0 ? "write" : "read";
        const auto id = flow_id(source, operation);
        // An AHB face holds its data phase with the read engine: HADDR, HWRITE, HSIZE, HBURST,
        // HPROT, HMASTLOCK and NONSEQ.
        const unsigned face =
            sp.ahb && direction == 1
                ? sp.address_bits + 13 +
                      (sp.ahb5 ? sp.hprot_bits - 4 + unsigned(sp.hnonsec) + sp.hmaster_bits : 0)
                : 0;
        // A mode-dependent decode holds the plane of a write in service.
        const unsigned plane =
            direction == 0 && plan.decodes_modes(static_cast<unsigned>(&sp - plan.sources.data()))
                ? plan.plane_bits
                : 0;
        resource(id, "burst_source_engine",
                 source_state.source_engine[direction] + half(sp.clamp, direction) +
                     half(sp.packets, direction) + face + plane,
                 "external burst", "the burst's response stream is consumed at the AXI face");
        resource(id + "/return", "burst_return", source_state.source_return[direction], id,
                 "response face consumption");
        resource(id + "/consumer", "environment", 0, "initiator",
                 "legal external response consumption");
        wait(id, id + "/return", CrossWaitKind::Completion,
             sp.outstanding > 1
                 ? "the slot table holds each burst identity until its response is consumed; "
                   "the bursts of one AXI ID share one route, so a response reaching the staging "
                   "is the oldest of its ID and the staging stays private to the flow"
                 : "the engine holds the burst identity until its response stream reaches the "
                   "staging; one outstanding burst per flow keeps the staging private to the flow");
        wait(id + "/return", id + "/consumer", CrossWaitKind::Environment,
             "only this direction waits for its consumer");
        if (sp.pooled)
          wait(id, flow_id(source, direction == 0 ? "read" : "write") + "/return",
               CrossWaitKind::Capacity,
               "a burst starts while the source's transactions in flight, reads and writes "
               "together, are fewer than its capacity; a burst's answer reaching either "
               "direction's staging frees one, as does each piece of a narrow-split burst");
        wait(id, init, CrossWaitKind::Startup, "local initialization before business capture");
        wait(id + "/return", init, CrossWaitKind::Startup, "local initialization before capture");
        for (const auto &target : names(plan.targets))
          if (reaches(plan, source, target))
            wait(
                id, "burst_service/" + target + "/" + operation, CrossWaitKind::Capacity,
                "the burst's request fragments wait for the addressed target engine's consumption; "
                "the packet occupation holds the route for the whole burst");
        if (sp.qos.holds())
          wait(id, "source/" + source + "/limiter", CrossWaitKind::Capacity,
               "a request head waits while the charged budget exceeds the burst_bytes");
        if (sp.qos.picks())
          wait(id, "source/" + source + "/regulator", CrossWaitKind::Capacity,
               "a request head waits while a raise packet holds the source output");
      }
      if (sp.qos.picks()) {
        // Each context keeps its route, and the committed control slot freezes another.
        resource("source/" + source + "/regulator", "rate_regulator",
                 plan.transport_address_bits + plan.target_bits, "charged response bytes",
                 "a raise packet leaves through its switch outputs");
        graph.resources.back().capacity = sp.outstanding + 1;
        wait("source/" + source + "/regulator", "domain/" + plan.domain.name + "/clock",
             CrossWaitKind::Environment,
             "the arrival of every target takes a raise packet without waiting for its engine");
      }
      if (sp.qos.holds()) {
        resource("source/" + source + "/limiter", "rate_limiter", 0, "charged request bytes",
                 "the budget drains every clock edge");
        wait("source/" + source + "/limiter", "domain/" + plan.domain.name + "/clock",
             CrossWaitKind::Environment, "the budget drains every qualified clock edge");
      }
    }
  }
  void targets() {
    for (const auto &target : names(plan.targets)) {
      const auto service = "burst_service/" + target;
      const auto target_env = service + "/target";
      const bool ahb = ahb_target(plan, target);
      const bool apb = apb_target(plan, target);
      const bool lite = lite_target(plan, target);
      const bool apb2 = apb && plan.target(target).protocol == Protocol::Apb2;
      const bool shared_bus = ahb || apb || lite;
      for (unsigned direction = 0; direction < 2; ++direction) {
        const auto operation = direction == 0 ? "write" : "read";
        const auto engine = service + "/" + operation;
        resource(engine,
                 apb    ? "burst_apb_outlet"
                 : lite ? "burst_axi_lite_outlet"
                 : ahb  ? "burst_ahb_outlet"
                        : "burst_axi_outlet",
                 shared_bus ? dedicated_engine_bits(plan, target, direction)
                            : outlet_engine_bits(plan, target, direction),
                 "admitted burst", "the response stream is accepted by the response switches");
        if (shared_bus)
          wait(engine, service + "/bus", CrossWaitKind::Capacity,
               apb    ? "the beat waits for its shared APB SETUP/ACCESS transfer grant"
               : lite ? "the beat waits for its shared AXI4-Lite single-transfer grant"
                      : "the beat waits for its shared AHB address/data transfer grant");
        else
          wait(engine, target_env, CrossWaitKind::Environment,
               "the AXI burst waits for its target response on this direction, or the target "
               "timeout");
        wait(engine, init, CrossWaitKind::Startup, "local initialization before business capture");
        for (const auto &source : names(plan.sources))
          if (reaches(plan, source, target))
            wait(engine, flow_id(source, operation) + "/return", CrossWaitKind::ReservedWrite,
                 "the admitted burst's response stream writes the matching flow's response "
                 "staging; the staging is reserved by the engine's single-outstanding "
                 "lifecycle, whose response readiness is a function of the flow's own state "
                 "and the external consumer");
      }
      if (shared_bus) {
        resource(
            service + "/bus",
            apb    ? "burst_apb_grant"
            : lite ? "burst_axi_lite_grant"
                   : "burst_ahb_grant",
            dedicated_bus_bits(plan, target),
            apb || lite ? "one read or write beat" : "one read or write subtransfer",
            apb2 ? "PSEL && PENABLE releases the bus after one ACCESS cycle independently of "
                   "response consumption"
            : apb
                ? "PSEL && PENABLE && PREADY releases the bus independently of response consumption"
            : lite ? "the accepted B or R response releases the bus independently of response "
                     "consumption"
                   : "the completed AHB data phase releases the bus independently of response "
                     "consumption");
        wait(service + "/bus", target_env, CrossWaitKind::Environment,
             apb2   ? "PSEL && PENABLE"
             : apb  ? "PSEL && PENABLE && PREADY"
             : lite ? "the AXI4-Lite B or R handshake"
                    : "legal AHB data phase completion");
        wait(service + "/bus", init, CrossWaitKind::Startup,
             "local reset release before bus issue");
      }
      resource(target_env, "environment", 0, "target",
               apb2   ? "PSEL && PENABLE"
               : apb  ? "PSEL && PENABLE && PREADY"
               : lite ? "the AXI4-Lite B or R handshake"
               : ahb  ? "legal AHB data phase completion"
                      : "legal AXI burst completion");
    }
  }
  void grants() {
    // A grant holds a read or a write packet; its destinations take both.
    for (bool response : {false, true}) {
      const auto &g = response ? plan.exchange.response : plan.exchange.request;
      for (const auto &o : g.outputs)
        if (o.inputs.size() > 1) {
          const auto id = std::string(response ? "graph/rsp/" : "graph/req/") + o.node + ">" +
                          (o.endpoint ? "endpoint/" : "node/") + o.output;
          resource(id, "packet_grant", 0, "selected input packet", "tail fragment accepted");
          wait(id, init, CrossWaitKind::Startup, "common reset release");
          for (const auto &destination : o.destinations)
            for (const auto *operation : {"write", "read"})
              wait(id,
                   response ? flow_id(destination, operation) + "/return"
                            : "burst_service/" + destination + "/" + operation,
                   response ? CrossWaitKind::ReservedWrite : CrossWaitKind::Capacity,
                   "packet ownership releases after the selected destination accepts its tail");
        }
    }
  }
  void links() {
    // Link stages are inventoried without wait edges. A serializer or deserializer subdivides one
    // segment's message-level transfer wait without adding a resource any other flow can wait on;
    // pipes are outside the current deadlock witness scope and must not inherit a certificate for
    // an unbuffered link. The clock placement adds the crossings.
    const auto burst = transport_burst(plan);
    for (const auto &link : plan.links)
      for (const auto &stage : link.stages)
        if (stage.kind == StageKind::Adapter)
          resource(stage.name, "edge_pipeline",
                   adapter_payload_bits(stage, plan.source_bits, burst), "in-flight fragment",
                   "downstream fragment acceptance");
        else if (stage.kind == StageKind::Pipe) {
          const auto fields =
              fragment_fields(stage.response, stage.wide_bytes, plan.source_bits, burst);
          resource(stage.name, "link_pipeline",
                   std::accumulate(fields.begin(), fields.end(), 0U,
                                   [](unsigned sum, const auto &f) { return sum + f.second; }),
                   "in-flight fragment", "downstream fragment acceptance");
          graph.resources.back().capacity =
              stage.pipe.half_rate ? 1U : unsigned{stage.pipe.forward} + stage.pipe.backward;
          graph.resources.back().pipe = stage.pipe;
        }
    // Endpoint pipes likewise: a slot of each mode.
    auto endpoint = [&](const std::vector<EndpointPipe> &pipes) {
      for (const auto &pipe : pipes) {
        resource(pipe.name, "endpoint_pipeline", pipe.bits, "in-flight word",
                 "downstream word acceptance");
        graph.resources.back().capacity = unsigned{pipe.mode.forward} + pipe.mode.backward;
        graph.resources.back().pipe = pipe.mode;
      }
    };
    for (const auto &s : plan.sources)
      endpoint(s.pipes);
    for (const auto &t : plan.targets)
      endpoint(t.pipes);
  }
};
} // namespace

// Resources and waits for the burst profile, following the joint waiting template: request
// inventory persists until its response stream is consumed. Dedicated AHB directions aggregate
// their packet contexts and reserved responses here; these resources describe storage and do not
// explore their queue scheduling. The service face is an environment terminal.
CrossGraph build_axi4_exchange_obligations(const NetworkPlan &plan) {
  Obligations o{plan};
  const auto clock = "domain/" + plan.domain.name + "/clock";
  o.resource(clock, "environment", 0, "integration", "qualified clock edge");
  o.resource(o.init, "release_chain", 0, "local reset", "local release terminal visible");
  // The divider counter of the single domain (clocked graphs add theirs in plan_clocking).
  if (plan.domain.type == ClockType::InternalDivider ||
      plan.domain.type == ClockType::EdgeQualified)
    o.resource("domain/" + plan.domain.name + "/divider", "clock_divider", 0, "clock port",
               "free-running division");
  o.wait(o.init, clock, CrossWaitKind::Environment,
         "local qualified edges and common reset release");
  o.sources();
  o.targets();
  o.grants();
  o.links();
  auto graph = std::move(o.graph);
  std::ranges::sort(graph.resources, {}, &CrossResource::id);
  std::ranges::sort(graph.waits, [](const auto &a, const auto &b) {
    return std::tie(a.from, a.to, a.kind, a.condition) <
           std::tie(b.from, b.to, b.kind, b.condition);
  });
  graph.waits.erase(std::unique(graph.waits.begin(), graph.waits.end()), graph.waits.end());
  return graph;
}
// Obligation checks for the burst profile: the budgets against independent
// lowerings of the components, then the graph against a fresh build.
Status verify_axi4_exchange_obligations(const NetworkPlan &plan, bool clocked) {
  auto fail = [](std::string message) {
    return Status::failure(
        error("NCL_EXCHANGE_OBLIGATION", "exchange.resources", std::move(message)));
  };
  if (std::ranges::any_of(plan.sources, [&](const auto &source) {
        const auto bits = source.engine_bits;
        return bits != plan.burst_data_bits &&
               !(plan.burst_data_bits == 32 &&
                 (bits == 64 || bits == 128 || bits == 256 || bits == 512 || bits == 1024));
      }))
    return fail("Missing or unsupported source width binding");
  // The transport occupancy holds the widest endpoint word and no more.
  {
    unsigned occupancy = 6;
    const auto widest = [&](unsigned bits) {
      occupancy = std::max(occupancy, static_cast<unsigned>(std::bit_width(bits / 8)));
    };
    if (plan.per_endpoint) {
      std::ranges::for_each(plan.sources, widest, &SourcePlan::engine_bits);
      std::ranges::for_each(plan.targets, widest, &TargetPlan::data_bits);
    }
    if (plan.occupancy_bits != occupancy)
      return fail("Transport occupancy width differs from the widest endpoint word");
  }
  // Merged targets are exactly the reached AXI4 targets in per-endpoint width mode, with
  // budgets matching an independent lowering of their merges.
  {
    const auto expected = upsize_targets(plan);
    for (unsigned ti = 0; ti < plan.targets.size(); ++ti) {
      const auto &t = plan.targets[ti];
      if (t.upsize.has_value() != expected.contains(t.name))
        return fail("Upsized targets differ from the reached AXI4 targets");
      if (t.upsize && axi4_upsize_state_bits(upsize_profile(plan, ti)) != *t.upsize)
        return fail("Upsize merge budget differs from its lowered state for " + t.name);
    }
  }
  // Packetized sources are exactly the AXI4 sources and clamped sources exactly those with a
  // narrowing route, with budgets matching an independent lowering of their splitters and clamps
  // (the clamp error mode only selects response data and adds no state, so it is not needed here).
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    const auto &s = plan.sources[si];
    if (s.packets.has_value() != (s.protocol != Protocol::Axi4Lite))
      return fail("Packetized sources differ from the AXI4 sources");
    if (s.packets && axi4_packetizer_state_bits(packet_profile(plan, si)) != *s.packets)
      return fail("Packet splitter budget differs from its lowered state for " + s.name);
    const bool narrowing = std::ranges::any_of(plan.exchange.request.routes, [&](const auto &r) {
      return plan.per_endpoint && r.entry == s.name &&
             endpoint_word(plan, r.entry) > endpoint_word(plan, r.exit);
    });
    if (s.clamp.has_value() != narrowing)
      return fail("Width clamp sources differ from the narrowing routes");
    if (s.clamp && axi4_width_clamp_state_bits(clamp_profile(plan, si)) != *s.clamp)
      return fail("Width clamp budget differs from its lowered state for " + s.name);
  }
  if (std::ranges::any_of(plan.targets, [&](const TargetPlan &t) {
        return (t.protocol != Protocol::Axi4 && !dedicated_protocol(t.protocol)) ||
               (dedicated_protocol(t.protocol) && plan.burst_data_bits != 32 &&
                t.protocol != Protocol::Axi4Lite);
      }))
    return fail("Invalid target protocol binding");
  auto ordered = order_cross_domain_graph(plan.graph);
  if (!ordered)
    return Status::failure(ordered.diagnostics());
  std::map<std::string, const CrossResource *> resources;
  if (std::ranges::any_of(plan.graph.resources,
                          [&](const auto &r) { return !resources.emplace(r.id, &r).second; }))
    return fail("Resource identities must be unique");
  // The graph is a fresh build from the plan.
  if ((clocked ? clocked_obligations(plan) : build_axi4_exchange_obligations(plan)) != plan.graph)
    return fail("Resources or waits differ from a fresh build of the plan");
  if (std::ranges::any_of(plan.graph.waits, [&](const auto &w) {
        return w.kind == CrossWaitKind::ReservedWrite && resources.at(w.to)->role != "burst_return";
      }))
    return fail("A reserved write requires a lifecycle-reserved return staging");
  return Status::success({});
}
} // namespace nocloom::detail
