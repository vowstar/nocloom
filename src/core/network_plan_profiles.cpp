#include "network_plan_profiles.hpp"
#include "capability.hpp"
#include "diagnostic.hpp"
#include <algorithm>
#include <bit>
#include <iterator>
#include <map>
#include <numeric>
#include <set>
#include <string>
#include <tuple>

namespace nocloom::detail {
namespace {
std::optional<unsigned> source_index(const NetworkPlan &plan, std::string_view source) {
  const auto at = std::ranges::lower_bound(plan.sources, source, {}, &SourcePlan::name);
  if (at == plan.sources.end() || at->name != source)
    return std::nullopt;
  return static_cast<unsigned>(at - plan.sources.begin());
}
std::optional<unsigned> target_position(const NetworkPlan &plan, std::string_view target) {
  const auto at = std::ranges::lower_bound(plan.targets, target, {}, &TargetPlan::name);
  if (at == plan.targets.end() || at->name != target)
    return std::nullopt;
  return static_cast<unsigned>(at - plan.targets.begin());
}
} // namespace
bool reaches(const NetworkPlan &plan, const std::string &source, const std::string &target) {
  const auto si = source_index(plan, source);
  const auto ti = target_position(plan, target);
  if (!si || !ti)
    return false;
  return plan.route_index.reaching[*si][*ti].reached;
}
std::uint64_t all_flows(const SourcePlan &source) {
  return (std::uint64_t{1} << std::max<std::size_t>(1, source.flows.size())) - 1;
}
std::uint64_t reaching_flows(const NetworkPlan &plan, const std::string &source,
                             const std::string &target, unsigned target_path) {
  const auto si = source_index(plan, source);
  const auto ti = target_position(plan, target);
  if (!si || !ti)
    return 0;
  const auto &reached = plan.route_index.reaching[*si][*ti];
  return target_path < reached.flows.size() ? reached.flows[target_path] : 0;
}
namespace {
// Global bases: a mode-dependent mapping counts at its own address, not in its plane.
std::pair<std::uint64_t, std::uint64_t> mapping_key(const NetworkPlan &plan, const MappingPlan &m) {
  return {m.window.size, m.window.base - (std::uint64_t{m.plane} << plan.plane_shift)};
}
// The smallest mapping of a target that some flow of the source reaches, the lowest based among
// equal sizes.
const MappingPlan *smallest_reached(const NetworkPlan &plan, const std::string &source,
                                    unsigned target) {
  const MappingPlan *best = nullptr;
  const auto bits = plan.sources[*source_index(plan, source)].address_bits;
  for (const auto &m : plan.targets[target].mappings)
    if (mapping_key(plan, m).second >> bits == 0 &&
        reaching_flows(plan, source, plan.targets[target].name, m.flow) &&
        (!best || mapping_key(plan, m) < mapping_key(plan, *best)))
      best = &m;
  return best;
}
} // namespace
// The boundary of a target's protocol: AXI min(4 KiB, local space), AHB min(1 KiB, local space),
// APB and AXI4-Lite the local space.
static std::uint64_t protocol_boundary(const TargetPlan &t) {
  const std::uint64_t space = std::uint64_t{1} << t.local_bits;
  return t.protocol == Protocol::Axi4    ? std::min<std::uint64_t>(4096, space)
         : t.protocol == Protocol::AhbV3 ? std::min<std::uint64_t>(1024, space)
                                         : space;
}
// The naturally aligned power-of-two blocks of a window: a target mapping of one base and mask.
std::vector<AddressWindow> aligned_blocks(const AddressWindow &window) {
  std::vector<AddressWindow> blocks;
  for (auto at = window.base, end = at + window.size; at < end; at += blocks.back().size) {
    auto size = std::bit_floor(end - at);
    if (at)
      size = std::min(size, at & (~at + 1));
    blocks.push_back({at, size});
  }
  return blocks;
}
// Decode misses use the smallest reached aligned block, then its lowest global address.
std::optional<DecodeEntry> select_default_mapping(std::span<const DecodeEntry> mappings,
                                                  unsigned address_bits) {
  using Key = std::tuple<std::uint64_t, std::uint64_t, unsigned>;
  std::optional<std::pair<Key, DecodeEntry>> best;
  for (const auto &m : mappings)
    for (const auto &[at, size] : aligned_blocks(m.route))
      if (address_bits >= 64 || at >> address_bits == 0) {
        const Key key{size, at, m.target};
        if (!best || key < best->first)
          best = {key, m};
      }
  return best ? std::optional{best->second} : std::nullopt;
}
static std::optional<std::pair<unsigned, std::size_t>> derive_implicit_miss(const NetworkPlan &plan,
                                                                            unsigned index) {
  std::vector<DecodeEntry> mappings;
  for (const auto &pair : plan.pairs)
    if (pair.source == index)
      for (unsigned m = 0; m < plan.targets[pair.target].mappings.size(); ++m) {
        const auto &t = plan.targets[pair.target];
        const auto &mp = t.mappings[m];
        auto window = mp.window;
        window.base -= std::uint64_t{mp.plane} << plan.plane_shift;
        if (reaching_flows(plan, plan.sources[index].name, t.name, mp.flow))
          mappings.push_back({window, pair.target, m});
      }
  const auto best = select_default_mapping(mappings, plan.sources[index].address_bits);
  return best ? std::optional{std::pair<unsigned, std::size_t>{best->target, best->mapping}}
              : std::nullopt;
}
static std::optional<MissRoute> derive_miss_route(const NetworkPlan &plan, unsigned index) {
  const auto &source = plan.sources[index];
  std::optional<unsigned> target;
  if (source.default_target)
    target = plan.target_index(*source.default_target);
  else if (const auto at = plan.route_index.implicit_misses[index])
    target = at->first;
  if (!target)
    return std::nullopt;
  const auto &tp = plan.targets[*target];
  const auto implicit =
      source.default_target ? std::nullopt : plan.route_index.implicit_misses[index];
  const auto flow = implicit ? tp.mappings[implicit->second].flow
                             : smallest_reached(plan, source.name, *target)->flow;
  const auto flows = reaching_flows(plan, source.name, tp.name, flow);
  return MissRoute{*target, tp.routes(tp.flows > 1 ? std::optional{flow} : std::nullopt).front(),
                   flow, static_cast<unsigned>(std::countr_zero(flows))};
}
// log2 of a count, rounded up: the bits of a number below it.
static unsigned number_bits(std::uint64_t count) {
  return count < 2 ? 0U : static_cast<unsigned>(std::bit_width(count - 1));
}
// Name order gives every endpoint a contiguous interval without padding.
static unsigned index_flows(const std::map<std::string, unsigned> &flows,
                            std::map<std::string, unsigned> &numbers) {
  unsigned next = 0;
  for (const auto &[name, count] : flows) {
    numbers[name] = next;
    next += count;
  }
  return next;
}
// Whether windows leave a hole in the first `bits` bits of the address space.
static bool leaves_hole(std::vector<AddressWindow> windows, unsigned bits) {
  std::ranges::sort(windows, {}, &AddressWindow::base);
  const auto top = std::uint64_t{1} << bits;
  std::uint64_t reached = 0;
  for (const auto &w : windows) {
    if (w.base > reached)
      return true;
    reached = std::max(reached, w.base + w.size);
  }
  return reached < top;
}
// The aligned blocks of a mapping a source decodes, at the mapping's own address: a block above the
// source's address space is none of its apertures.
static std::vector<AddressWindow> source_blocks(const NetworkPlan &plan, const MappingPlan &m,
                                                unsigned source_bits) {
  auto blocks =
      aligned_blocks({m.window.base - (std::uint64_t{m.plane} << plan.plane_shift), m.window.size});
  std::erase_if(blocks, [&](const AddressWindow &b) { return b.base >> source_bits != 0; });
  return blocks;
}
// A mode condition's place among others on one block: the flags it requires by name, then those it
// excludes by name. A list that ends comes after any name, so a condition that says more goes first
// and no condition goes last.
struct ConditionOrder {
  std::vector<std::string> required, excluded;
  static bool before(const std::vector<std::string> &a, const std::vector<std::string> &b) {
    const auto [at, there] = std::ranges::mismatch(a, b);
    return at != a.end() && (there == b.end() || *at < *there);
  }
  bool operator<(const ConditionOrder &o) const {
    return before(required, o.required) ||
           (!before(o.required, required) && before(excluded, o.excluded));
  }
  bool operator==(const ConditionOrder &) const = default;
};
// Where a block stands among a source's blocks: smallest first, the lowest base of that size, then
// the mode condition of its mapping.
using DecodeRank = std::tuple<unsigned, std::uint64_t, ConditionOrder>;
static DecodeRank block_rank(const AddressWindow &block, const MappingPlan &m) {
  ConditionOrder order;
  for (const auto &[flag, value] : m.when)
    (value ? order.required : order.excluded).push_back(flag);
  return {static_cast<unsigned>(std::countr_zero(block.size)), block.base, std::move(order)};
}
// Apertures a flow of a source adds for its decode misses when it has a default error target: one
// per set of blocks some assignment of the flags in their conditions leaves visible, when that set
// leaves an address unmapped. Assignments with the same blocks have the same holes.
static unsigned error_apertures(const NetworkPlan &plan,
                                const std::vector<const MappingPlan *> &mappings, unsigned bits) {
  struct Block {
    AddressWindow window;
    const std::map<std::string, bool> *when = nullptr;
  };
  std::vector<Block> blocks;
  std::set<std::string> flags;
  for (const auto *m : mappings) {
    std::ranges::transform(source_blocks(plan, *m, bits), std::back_inserter(blocks),
                           [&](const AddressWindow &block) { return Block{block, &m->when}; });
    std::ranges::transform(m->when, std::inserter(flags, flags.end()),
                           [](const auto &term) { return term.first; });
  }
  if (blocks.empty())
    return 0;
  const std::vector<std::string> flag_list(flags.begin(), flags.end());
  std::set<std::vector<bool>> seen;
  unsigned holes = 0;
  for (std::uint32_t values = 0; values < std::uint32_t{1} << flag_list.size(); ++values) {
    std::vector<bool> visible;
    std::vector<AddressWindow> windows;
    for (const auto &block : blocks) {
      const bool shown = std::ranges::all_of(*block.when, [&](const auto &term) {
        const auto at = std::ranges::lower_bound(flag_list, term.first) - flag_list.begin();
        return ((values >> at) & 1U) == (term.second ? 1U : 0U);
      });
      visible.push_back(shown);
      if (shown)
        windows.push_back(block.window);
    }
    if (seen.insert(std::move(visible)).second && leaves_hole(std::move(windows), bits))
      ++holes;
  }
  return holes;
}
static TargetIdNumbering derive_target_ids(const NetworkPlan &plan) {
  TargetIdNumbering out;
  unsigned region_count = 1;
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    const auto &source = plan.sources[si];
    std::uint64_t used_flows = 0;
    // Collect reachable mapping domains once, independently of the endpoint numbering.
    std::map<std::pair<unsigned, unsigned>, std::vector<const MappingPlan *>> domains;
    for (unsigned ti = 0; ti < plan.targets.size(); ++ti) {
      const auto &target = plan.targets[ti];
      const auto &reach = plan.route_index.reaching[si][ti];
      if (!reach.reached)
        continue;
      for (unsigned g = 0; g < reach.flows.size(); ++g) {
        used_flows |= reach.flows[g];
        if (reach.flows[g])
          out.target_paths[target.name] = std::max(out.target_paths[target.name], g + 1);
      }
      for (const auto &mapping : target.mappings)
        if (reach.flows[mapping.flow])
          domains[{ti, mapping.flow}].push_back(&mapping);
    }
    if (!used_flows)
      continue;
    out.source_paths[source.name] = static_cast<unsigned>(std::bit_width(used_flows));
    out.sequence_bits = std::max(out.sequence_bits, source.ahb ? 0U : source.id_bits.value_or(0));
    out.direction_bits = std::max(out.direction_bits, source.apb ? 0U : 1U);
    // Observation regions enumerate address blocks for each connected flow pair. They do not
    // participate in a transaction identity.
    for (unsigned f = 0; f < out.source_paths.at(source.name); ++f) {
      std::vector<const MappingPlan *> visible;
      std::map<std::pair<unsigned, unsigned>, unsigned> counts;
      for (const auto &[domain, mappings] : domains) {
        const auto [ti, g] = domain;
        if (!((plan.route_index.reaching[si][ti].flows[g] >> f) & 1))
          continue;
        auto &count = counts[domain];
        for (const auto *mapping : mappings) {
          visible.push_back(mapping);
          count += static_cast<unsigned>(source_blocks(plan, *mapping, source.address_bits).size());
        }
      }
      const auto holes = error_apertures(plan, visible, source.address_bits);
      const auto miss = plan.route_index.misses[si];
      for (const auto &[domain, count] : counts) {
        const bool catches_miss =
            counts.size() == 1 ||
            (source.default_target && miss && domain == std::pair{miss->target, miss->target_path});
        region_count = std::max(region_count, count + (catches_miss ? holes : 0));
      }
    }
  }
  out.initiator_bits = number_bits(index_flows(out.source_paths, out.initiators));
  out.target_bits = number_bits(index_flows(out.target_paths, out.targets));
  out.range_bits = number_bits(region_count);
  return out;
}
// What a miss keeps of its address for an error log: a source of one target needs no address to
// steer it and keeps all of it; another keeps the bits of the first block its walk finds, and
// steers there.
static Axi4InitiatorProfile::MissKept kept_of_miss(const NetworkPlan &plan, unsigned si) {
  if (std::ranges::count_if(plan.route_index.reaching[si], &TargetReach::reached) == 1)
    return {0, ~std::uint64_t{0}};
  const auto at = plan.route_index.implicit_misses[si];
  const auto &mapping = plan.targets[at->first].mappings[at->second];
  auto blocks = source_blocks(plan, mapping, plan.sources[si].address_bits);
  const auto &block = *std::ranges::min_element(
      blocks, {}, [](const AddressWindow &b) { return std::pair{b.size, b.base}; });
  return {block.base, block.size - 1};
}
// The routing windows of the targets a source reaches.
static std::vector<AddressWindow> reached_windows_of(const NetworkPlan &plan, unsigned si) {
  std::vector<AddressWindow> out;
  for (unsigned ti = 0; ti < plan.targets.size(); ++ti)
    if (plan.route_index.reaching[si][ti].reached)
      std::ranges::copy(plan.targets[ti].routes(), std::back_inserter(out));
  return out;
}
// A packet's length field holds the longest burst of a source's word.
unsigned record_bytes_minus_one_bits(const NetworkPlan &plan) {
  unsigned bits = 2;
  for (const auto &source : plan.sources)
    if (source.len_bits)
      bits = std::max(bits, source.len_bits.value_or(0) +
                                static_cast<unsigned>(std::countr_zero(source.engine_bits / 8)));
  return bits;
}
// Transfer only the fields present in the stored record.
Result<FaultMonitorPlan> plan_fault_monitor(const NetworkPlan &plan,
                                            const FaultMonitorRequest &request) {
  constexpr unsigned code_bits = 3, least_address_bits = 8;
  const auto length_bits = record_bytes_minus_one_bits(plan);
  const auto &ids = plan.route_index.target_ids;
  FaultMonitorPlan out;
  out.logger.name = request.name;
  out.logger.targets = static_cast<unsigned>(request.targets.size());
  out.logger.ignored_codes = request.ignored_codes;
  out.service = plan.target_index(request.service);
  out.range_shift = ids.shift("region_index", true);
  out.keep_fields = request.keep_fields;
  const auto route_bits = ids.record_bits();
  unsigned address_bits = least_address_bits;
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    const auto &source = plan.sources[si];
    std::vector<const MappingPlan *> all;
    unsigned reached = 0;
    for (unsigned ti = 0; ti < plan.targets.size(); ++ti)
      if (plan.route_index.reaching[si][ti].reached) {
        ++reached;
        for (const auto &m : plan.targets[ti].mappings) {
          all.push_back(&m);
          const auto blocks = source_blocks(plan, m, source.address_bits);
          address_bits = std::accumulate(
              blocks.begin(), blocks.end(), address_bits,
              [](unsigned bits, const AddressWindow &block) {
                return std::max(bits, static_cast<unsigned>(std::countr_zero(block.size)));
              });
        }
      }
    if (reached == 1 && error_apertures(plan, all, source.address_bits) != 0)
      address_bits = std::max(address_bits, source.address_bits);
  }
  for (const auto &name : request.targets) {
    const auto ti = plan.target_index(name);
    ObservedTarget seen;
    seen.target = ti;
    for (unsigned si = 0; si < plan.sources.size(); ++si) {
      const auto &source = plan.sources[si];
      if (!plan.route_index.reaching[si][ti].reached)
        continue;
      ObservedSource row;
      row.source = si;
      row.keeps_id = !source.apb;
      row.route =
          (std::uint64_t{ids.initiators.at(source.name)} << ids.shift("source_path", true)) |
          (std::uint64_t{ids.targets.at(name)} << ids.shift("target_path", true));
      std::vector<std::pair<DecodeRank, AddressWindow>> blocks;
      for (const auto &m : plan.targets[ti].mappings) {
        const auto own = source_blocks(plan, m, source.address_bits);
        std::ranges::transform(own, std::back_inserter(blocks), [&](const AddressWindow &block) {
          return std::pair{block_rank(block, m), block};
        });
      }
      std::ranges::sort(blocks, {}, [](const auto &b) { return b.first; });
      std::ranges::transform(blocks, std::back_inserter(row.blocks), [&](const auto &block) {
        return ObservedBlock{block.second.base, block.second.size,
                             static_cast<unsigned>(row.blocks.size())};
      });
      unsigned reached = 0;
      for (unsigned other = 0; other < plan.targets.size(); ++other)
        reached += plan.route_index.reaching[si][other].reached ? 1 : 0;
      if (miss_route(plan, source.name)) {
        row.miss_mask = kept_of_miss(plan, si).bits;
        row.miss_range = reached == 1 ? static_cast<unsigned>(blocks.size()) : 0;
      }
      seen.sources.push_back(std::move(row));
    }
    out.observed.push_back(std::move(seen));
  }
  const auto code = plan.error_codes ? code_bits : 0U;
  // A record has room for every user flag either way (without keep_fields only a miss fills it).
  const auto user_bits = plan.burst_user_bits;
  std::vector<std::string> filtered_users;
  if (request.keep_fields)
    filtered_users = plan.user_flags;
  // A logger's filter compares bits of a record: a flag's bit is its place in its declaration.
  const auto filter_bits = [&](const std::map<std::string, bool> &wanted,
                               const std::vector<std::string> &names) {
    std::vector<RecordFilterBit> bits;
    for (const auto &[flag, value] : wanted) {
      const auto at = std::ranges::find(names, flag);
      if (at == names.end())
        return std::optional<std::vector<RecordFilterBit>>{};
      bits.push_back({static_cast<unsigned>(at - names.begin()), value});
    }
    return std::optional{std::move(bits)};
  };
  const auto user_filter = filter_bits(request.user_filter, filtered_users);
  const auto security_filter = filter_bits(request.security_filter, plan.security_flags);
  if (!user_filter || !security_filter)
    return Result<FaultMonitorPlan>::failure(
        error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + request.name,
              "A filter names flags a record keeps: audit flags, and user flags when it keeps "
              "its fields"));
  out.logger.user_filter = *user_filter;
  out.logger.security_filter = *security_filter;
  out.logger.pass_always = request.forward;
  for (const auto &next : request.chain) {
    const auto users = filter_bits(next.user_filter, filtered_users);
    const auto flags = filter_bits(next.security_filter, plan.security_flags);
    if (!users || !flags)
      return Result<FaultMonitorPlan>::failure(
          error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + request.name,
                "A filter names flags a record keeps: audit flags, and user flags when it keeps "
                "its fields"));
    out.logger.chain.push_back({next.ignored_codes, *users, *flags, next.forward});
  }
  const auto security_bits = plan.burst_security_bits;
  out.logger.shape = {route_bits, address_bits, user_bits, security_bits, length_bits, code};
  out.logger.fixed_length = length_bits == 2 ? 3 : 0;
  out.logger.serial_word_bits = request.serial_word_bits;
  if (request.serial_word_bits != 8 && request.serial_word_bits != 16 &&
      request.serial_word_bits != 32)
    return Result<FaultMonitorPlan>::failure(
        error("NCL_INTERNAL_PLAN", "error_logging." + request.name, "Expected 8, 16 or 32 bits"));
  out.logger.serial_words = error_record_layout(out.logger).words(request.serial_word_bits);
  return Result<FaultMonitorPlan>::success(std::move(out));
}
void build_route_index(NetworkPlan &plan) {
  auto &index = plan.route_index;
  index.reaching.resize(plan.sources.size());
  for (std::size_t si = 0; si < plan.sources.size(); ++si) {
    index.reaching[si].resize(plan.targets.size());
    for (std::size_t ti = 0; ti < plan.targets.size(); ++ti)
      index.reaching[si][ti].flows.resize(plan.targets[ti].flows);
  }
  for (const auto &route : plan.exchange.request.routes) {
    const auto si = *source_index(plan, route.entry);
    const auto ti = plan.target_index(route.exit);
    auto &reached = index.reaching[si][ti];
    reached.reached = true;
    reached.flows[route.target_path.value_or(0)] |=
        route.flows.empty()
            ? all_flows(plan.sources[si])
            : std::accumulate(route.flows.begin(), route.flows.end(), std::uint64_t{0},
                              [](std::uint64_t mask, unsigned flow) {
                                return mask | std::uint64_t{1} << flow;
                              });
  }
  index.implicit_misses.resize(plan.sources.size());
  index.misses.resize(plan.sources.size());
  for (unsigned si = 0; si < plan.sources.size(); ++si)
    index.implicit_misses[si] = derive_implicit_miss(plan, si);
  for (unsigned si = 0; si < plan.sources.size(); ++si)
    index.misses[si] = derive_miss_route(plan, si);
  index.target_ids = derive_target_ids(plan);
}
// The target that answers a source's decode misses: the one it names, else the owner of its
// implicit_miss mapping; none when it reaches none.
std::optional<unsigned> default_target(const NetworkPlan &plan, const std::string &source) {
  const auto index = source_index(plan, source);
  if (!index)
    return std::nullopt;
  if (plan.sources[*index].default_target)
    return plan.target_index(*plan.sources[*index].default_target);
  if (const auto at = plan.route_index.implicit_misses[*index])
    return at->first;
  return std::nullopt;
}
std::optional<MissRoute> miss_route(const NetworkPlan &plan, const std::string &source) {
  const auto index = source_index(plan, source);
  return index ? plan.route_index.misses[*index] : std::nullopt;
}
// Indices of sources with a request route to this target, in ascending source order.
std::vector<unsigned> reaching_sources(const NetworkPlan &plan, const std::string &target) {
  std::vector<unsigned> out;
  const auto ti = plan.target_index(target);
  for (unsigned si = 0; si < plan.sources.size(); ++si)
    if (plan.route_index.reaching[si][ti].reached)
      out.push_back(si);
  return out;
}
// The exclusive windows of one source (axi4_initiator.hpp): the own window of every mapping of a
// target with exclusive support it reaches, with that target's byte limit. An exclusive access
// never exceeds 128 bytes nor 2^len target words (an AXI4 target's AxLEN, else the network length
// field), and at an AHB target not 16 target words (the AHB packet limit: an exclusive burst is
// never cut, and a burst past the limit would have to be). A target without write strobes (APB3,
// APB2) takes no exclusive beat narrower than its word, whose write would fail as a partial word.
std::vector<ExclusiveWindow> exclusive_windows(const NetworkPlan &plan, const std::string &source) {
  std::vector<ExclusiveWindow> out;
  for (const auto &t : plan.targets) {
    if (t.exclusive.mode == ExclusiveAccess::None || !reaches(plan, source, t.name))
      continue;
    const unsigned word = dedicated_protocol(t.protocol) && t.protocol != Protocol::AhbV3 &&
                                  t.protocol != Protocol::Axi4Lite
                              ? 4U
                              : endpoint_word(plan, t.name);
    unsigned limit = std::min(128U, (1U << t.len_bits.value_or(plan.burst_len_bits)) * word);
    if (t.protocol == Protocol::AhbV3 || t.exclusive.mode == ExclusiveAccess::Target)
      limit = std::min(limit, 16 * word);
    const bool strobeless = t.protocol == Protocol::Apb3 || t.protocol == Protocol::Apb2;
    std::ranges::transform(t.routes(), std::back_inserter(out), [&](const AddressWindow &w) {
      return ExclusiveWindow{w, static_cast<unsigned>(std::min<std::uint64_t>(limit, w.size)),
                             strobeless ? 2U : 0U};
    });
  }
  return out;
}
bool ahb_target(const NetworkPlan &plan, const std::string &target) {
  return plan.target(target).protocol == Protocol::AhbV3;
}
bool apb_target(const NetworkPlan &plan, const std::string &target) {
  return protocol_caps(plan.target(target).protocol).apb;
}
// Per-endpoint attachment takes a source's engine width and a target's data width.
unsigned endpoint_word(const NetworkPlan &plan, const std::string &endpoint) {
  const auto source = std::ranges::find(plan.sources, endpoint, &SourcePlan::name);
  return !plan.per_endpoint             ? plan.burst_data_bits / 8
         : source != plan.sources.end() ? source->engine_bits / 8
                                        : plan.target(endpoint).data_bits / 8;
}
// The burst fields a profile of one endpoint takes from the plan.
template <typename Profile>
static Profile burst_fields(const NetworkPlan &plan, const std::string &name) {
  Profile profile;
  profile.name = name;
  profile.id_bits = plan.burst_id_bits;
  profile.len_bits = plan.burst_len_bits;
  profile.address_bits = plan.burst_address_bits;
  profile.source_bits = plan.source_bits;
  profile.user_bits = plan.burst_user_bits;
  profile.response_user_bits = plan.burst_response_user_bits;
  return profile;
}
// Width clamp of one source: every routing window carries its target's width, capped at the source
// width (a wider target passes the burst unchanged). With fixed_reject, a FIXED burst wider than
// its target that starts in the target's own window answers SLVERR instead of being cut, whatever
// the target protocol and fixed_bursts; a start in a sub-page remainder is cut, so the per-word
// decode answers DECERR without access. The refusals add no state, so budgets may omit them.
static bool reassembles_given(const NetworkPlan &plan, unsigned index,
                              const std::vector<bool> &interleaved) {
  const auto &source = plan.sources[index];
  // A target that keeps several bursts in flight may alternate this source's reads of different
  // IDs, whatever its width (a clamped source reaches a wider target through its merge). A narrower
  // one-burst target returns one read at a time, but its segments of a response run (message.hpp
  // point 18) interleave with those of other targets. With several bursts of its own, the source
  // takes a one-burst target's segments between the words of its other reads.
  if (source.write_only || source.outstanding_ids <= 1)
    return false;
  for (unsigned i = 0; i < plan.targets.size(); ++i) {
    const auto &t = plan.targets[i];
    if ((source.outstanding > 1 || t.outstanding > 1 ||
         (plan.response_runs && t.data_bits < source.engine_bits)) &&
        interleaved[i] && plan.route_index.reaching[index][i].reached)
      return true;
  }
  return false;
}
std::vector<bool> interleaved_targets(const NetworkPlan &plan) {
  std::vector<bool> out;
  std::ranges::transform(plan.targets, std::back_inserter(out),
                         [&](const auto &t) { return interleaved_target(plan, t); });
  return out;
}
bool clamp_reassembles(const NetworkPlan &plan, unsigned index) {
  return reassembles_given(plan, index, interleaved_targets(plan));
}
std::vector<bool> reassembling_sources(const NetworkPlan &plan,
                                       const std::vector<bool> &interleaved) {
  std::vector<bool> out;
  for (unsigned i = 0; i < plan.sources.size(); ++i)
    out.push_back(reassembles_given(plan, i, interleaved));
  return out;
}

Axi4WidthClampProfile clamp_profile(const NetworkPlan &plan, unsigned index, bool fixed_reject) {
  const auto &source = plan.sources[index];
  auto profile = burst_fields<Axi4WidthClampProfile>(plan, "s" + std::to_string(index));
  profile.data_bits = source.engine_bits;
  const unsigned source_log = std::bit_width(profile.data_bits / 8) - 1;
  for (const auto &t : plan.targets) {
    const auto width = std::min(
        source_log, static_cast<unsigned>(std::bit_width(endpoint_word(plan, t.name)) - 1));
    for (const auto &route : t.routes())
      profile.windows.emplace_back(route, width);
    if (t.len_bits && *t.len_bits < profile.len_bits)
      for (const auto &route : t.routes())
        profile.piece_len_bits.emplace_back(route, *t.len_bits);
    if (fixed_reject && width < source_log)
      std::ranges::copy(t.routes(), std::back_inserter(profile.fixed_reject));
  }
  // A wide read beat with a failed part returns zero data as a whole.
  profile.zero_failed_beats = true;
  profile.response_runs = plan.response_runs;
  profile.decode_packets = plan.error_codes;
  profile.decode_flag = decode_flag(plan);
  profile.exclusive = plan.exclusive;
  profile.max_outstanding = source.engine_slots;
  profile.tag_bits = profile.max_outstanding > 1 ? plan.burst_tag_bits : 0;
  profile.packet_depth = source.packets ? 4 : 1;
  profile.read_granule_taken = source.packets && reads_granule_target(plan, index);
  profile.reassembly = clamp_reassembles(plan, index);
  profile.read_hints = source.packets.has_value();
  profile.write_hints = profile.read_hints && !source.read_only;
  if (profile.read_hints)
    profile.source_miss = packet_profile(plan, index).source_miss;
  return profile;
}

// Packet defaults of a target: its data bytes as packets count them (APB 4), the packet limit (AXI
// min(2^AxLEN width words, 4 KiB), APB and AXI4-Lite 128 B, AHB min(16 words, 1 KiB)), which also
// bounds the WRAP it takes, and the boundary (protocol_boundary, then the largest power of two
// dividing the base and size of the mapping and the boundary of the block in it (block, zero for
// none), or of every mapping and block without one).
static PacketLimits target_packet_defaults(const NetworkPlan &plan, std::size_t i,
                                           std::optional<std::size_t> mapping = std::nullopt,
                                           std::uint64_t block = 0) {
  const auto &t = plan.targets[i];
  std::uint64_t bounds = block;
  for (std::size_t m = 0; m < t.mappings.size(); ++m)
    if (!mapping || m == *mapping) {
      bounds |= t.mappings[m].window.base | t.mappings[m].window.size;
      for (const auto &[_, boundary] : t.mappings[m].boundaries)
        bounds |= mapping ? 0 : boundary;
    }
  const bool axi = t.protocol == Protocol::Axi4, ahb = t.protocol == Protocol::AhbV3;
  PacketLimits out;
  out.bytes = axi || ahb || t.protocol == Protocol::Axi4Lite ? endpoint_word(plan, t.name) : 4;
  out.boundary_bytes = std::min(protocol_boundary(t), std::uint64_t{1} << std::countr_zero(bounds));
  out.max_bytes = axi   ? std::min<std::uint64_t>(out.bytes << t.len_bits.value(), 4096)
                  : ahb ? std::min<std::uint64_t>(16 * out.bytes, 1024)
                        : 128;
  return out;
}
// The largest WRAP container an AXI4 source sends, min(16, 2^AxLEN width) beats, and the largest
// it issues by default, max(min(64 B, that), 2 beats); zero for a source without WRAP (AXI4-Lite,
// APB, a zero-bit AxLEN).
static std::uint64_t wrap_container(const SourcePlan &source) {
  const unsigned len = source.len_bits.value_or(0);
  return len ? (source.engine_bits / 8) << std::min(len, 4U) : 0;
}
static std::uint64_t source_wrap_limit(const SourcePlan &s) {
  const auto most = wrap_container(s);
  return std::max<std::uint64_t>(std::min<std::uint64_t>(64, most), most ? s.engine_bits / 4 : 0);
}
// A pair's limits: the target defaults with the alignment word max(source bytes, target bytes) as
// bytes, narrowed by a packet limit within [alignment word, default]; error names a value outside
// that range. Without a mapping the default boundary holds at every mapping; with one, in the
// block whose boundary is block (zero: outside its blocks holding others).
PacketLimits pair_limits(const NetworkPlan &plan, const PairPlan &pair,
                         std::optional<std::size_t> mapping, std::uint64_t block) {
  auto out = target_packet_defaults(plan, pair.target, mapping, block);
  out.bytes = std::max<std::uint64_t>(plan.sources[pair.source].engine_bits / 8, out.bytes);
  out.max_bytes = std::max(out.max_bytes, out.bytes);
  for (auto [value, limit, name] :
       {std::tuple{pair.max_bytes, &out.max_bytes, "max_bytes"},
        std::tuple{pair.boundary_bytes, &out.boundary_bytes, "boundary_bytes"}})
    if (value && (value < out.bytes || value > *limit))
      out.error = std::string(name) + " " + std::to_string(value) + " is outside " +
                  std::to_string(out.bytes) + ".." + std::to_string(*limit) + " bytes";
    else if (value)
      *limit = value;
  return out;
}

// The network length field: the fewest bits, at least four, for every source AxLEN and every packet
// in its beats (an exclusive burst is never cut); zero when an AXI4 target's AxLEN cannot carry one
// beat of a wider source.
unsigned network_len_bits(const NetworkPlan &plan) {
  auto beats = std::accumulate(plan.sources.begin(), plan.sources.end(), std::uint64_t{16},
                               [](std::uint64_t most, const SourcePlan &s) {
                                 return std::max(most, std::uint64_t{1} << s.len_bits.value_or(0));
                               });
  for (const auto &pair : plan.pairs) {
    const auto &source = plan.sources[pair.source];
    const auto &target = plan.targets[pair.target];
    const unsigned slen = source.len_bits.value_or(0);
    const std::uint64_t bytes = source.engine_bits / 8;
    const auto word = target_packet_defaults(plan, pair.target).bytes;
    if (target.len_bits && bytes > word << *target.len_bits)
      return 0;
    auto packet =
        !source.len_bits ? bytes : std::min(bytes << slen, pair_limits(plan, pair).max_bytes);
    if (source.len_bits && target.exclusive.mode != ExclusiveAccess::None)
      packet = std::max(packet, std::min<std::uint64_t>(128, bytes << std::min(slen, 4U)));
    beats = std::max(beats, packet / std::min(bytes, word));
  }
  return static_cast<unsigned>(std::bit_width(beats - 1));
}

// Default routes split at exposed mapping boundaries and cap packets by target and source words.
static std::pair<std::uint64_t, std::uint64_t> named_miss_limits(const NetworkPlan &plan,
                                                                 unsigned index, unsigned target) {
  const auto &source = plan.sources[index];
  std::vector<AddressWindow> reached;
  for (const auto &pair : plan.pairs)
    if (pair.source == index)
      for (const auto &m : plan.targets[pair.target].mappings)
        if (!m.plane && m.window.base >> source.address_bits == 0 &&
            reaching_flows(plan, source.name, plan.targets[pair.target].name, m.flow))
          reached.push_back(m.window);
  std::set<std::uint64_t> ends;
  for (const auto &w : reached)
    ends.insert(w.base + w.size);
  const auto exposed =
      std::accumulate(reached.begin(), reached.end(), protocol_boundary(plan.targets[target]),
                      [&](std::uint64_t bits, const AddressWindow &w) {
                        return ends.contains(w.base) ? bits : bits | w.base;
                      });
  const auto boundary = exposed & (0 - exposed);
  const std::uint64_t word = target_packet_defaults(plan, target).bytes,
                      bytes = source.engine_bits / 8;
  const auto burst =
      std::min<std::uint64_t>(bytes << source.len_bits.value_or(0), source.ahb ? 1024 : 4096);
  const auto align = std::max(bytes, word);
  const auto most = std::min({256 * word, boundary, std::max(burst, word)});
  return {std::max(most, align), std::max(std::min<std::uint64_t>(boundary, 4096), align)};
}

bool exact_packets(const NetworkPlan &plan, unsigned source) {
  return std::ranges::any_of(plan.pairs, [&](const PairPlan &pair) {
    return pair.source == source && plan.targets[pair.target].read_side_effects;
  });
}

// Packet table of one source: per reached mapping, the pair's limits and the WRAP limit
// min(target WRAP limit, packet limit, source limit), in a cut page at most the mapping boundary.
// A target's WRAP limit is the largest power of two from two target words up to its packet limit,
// 16 words at an AXI4 target and the largest source limit reaching it, or none; a path limit of
// one target word is none. The source cuts WRAP bursts when a reached target has none or one below
// the source's (path value 1: every unaligned WRAP is cut).
namespace {
// A writing source with decode misses or refused writes drops their beats at its network entry
// (miss_drop), except a width-converting source with refused writes, whose splitter drops them;
// with request stages between its splitter and that entry (a write packets pipe, the issue pipe,
// a rate limiter or a regulator) a miss head reaches the entry later (miss_lag, used at capacity
// one).
void miss_drop_flags(const SourcePlan &source, Axi4PacketizerProfile &profile) {
  const auto missing = [](const Axi4PacketWindow &w) { return w.miss || w.refused; };
  const auto refusing = [](const Axi4PacketWindow &w) { return w.refused; };
  const bool converts = source.clamp.has_value();
  profile.miss_drop = profile.write &&
                      (profile.fallback || std::ranges::any_of(profile.windows, missing)) &&
                      !(converts && std::ranges::any_of(profile.windows, refusing));
  const auto staged = [](const EndpointPipe &pipe) {
    return pipe.name.ends_with("packets/write") || pipe.name.ends_with("issue");
  };
  profile.miss_lag = profile.miss_drop && profile.max_outstanding == 1 &&
                     (source.qos.shaper || std::ranges::any_of(source.pipes, staged));
}
} // namespace

bool wrap_aligned(const NetworkPlan &plan, unsigned source, unsigned target) {
  const auto &s = plan.sources[source];
  const auto &t = plan.targets[target];
  const auto dispatch = std::ranges::any_of(
      t.pipes, [](const EndpointPipe &e) { return e.name.ends_with("/pipe/dispatch"); });
  return s.protocol == Protocol::Axi4 && !s.ahb && !s.axi3 && !s.clamp &&
         t.protocol == Protocol::Axi4 && !t.axi3 && !t.upsize && !dispatch &&
         !t.read_side_effects && !t.write_only && s.engine_bits / 8 == endpoint_word(plan, t.name);
}

bool aligns_wrap_reads(const NetworkPlan &plan, unsigned target) {
  return std::ranges::any_of(plan.pairs, [&](const PairPlan &pair) {
    return pair.target == target && wrap_aligned(plan, pair.source, target);
  });
}

static std::uint64_t read_packing_bytes(const TargetPlan &target, std::uint64_t pack) {
  const bool apb = target.protocol == Protocol::Apb4 || target.protocol == Protocol::Apb3 ||
                   target.protocol == Protocol::Apb2;
  return apb ? std::min<std::uint64_t>(pack, target.apb_bits / 8) : pack;
}
Axi4PacketizerProfile packet_profile(const NetworkPlan &plan, unsigned index) {
  const auto &source = plan.sources[index];
  auto profile = burst_fields<Axi4PacketizerProfile>(plan, "s" + std::to_string(index));
  profile.data_bits = source.engine_bits;
  profile.split_narrow = source.split_narrow;
  profile.write = !source.read_only; // a read-only source has no write face to cut
  profile.level_bits = plan.burst_level_bits;
  profile.exclusive = plan.exclusive;
  profile.max_outstanding = source.engine_slots;
  profile.tag_bits = profile.max_outstanding > 1 ? plan.burst_tag_bits : 0;
  const auto source_wrap = source_wrap_limit(source), largest = wrap_container(source);
  const auto missed = *miss_route(plan, source.name);
  const auto fallback = missed.target;
  // The mapping a miss is cut as: the implicit_miss one, or a named default target's smallest.
  const auto implicit =
      source.default_target ? std::nullopt : plan.route_index.implicit_misses[index];
  // The source moves an admission miss into its miss window (initiator_profile) and hands its own
  // addresses over (address_hints).
  if (source.protocol != Protocol::Axi4Lite)
    profile.source_miss = missed.window;
  if (plan.observes_miss_address)
    profile.miss_kept_bits =
        kept_of_miss(plan, index).bits & ((std::uint64_t{1} << source.address_bits) - 1);
  profile.record_bits = plan.burst_record_bits;
  profile.security_bits = plan.burst_security_bits;
  // A width clamp behind plans its pieces from the hints.
  profile.packet_hints = source.clamp.has_value();
  bool across = false;
  Axi4PacketWindow miss{};
  std::vector<Axi4PacketWindow> outer;
  std::map<std::uint64_t, unsigned> pages; // cut pages: a target flow's group
  std::vector<std::uint64_t> wraps(plan.targets.size());
  for (const auto &pair : plan.pairs)
    wraps[pair.target] = std::max(wraps[pair.target], source_wrap_limit(plan.sources[pair.source]));
  // Unrouted windows never reach the splitter: the source answers them.
  for (const auto &pair : plan.pairs) {
    if (pair.source != index)
      continue;
    const auto &target = plan.targets[pair.target];
    const auto limits = pair_limits(plan, pair),
               defaults = target_packet_defaults(plan, pair.target);
    const auto sources = wraps[pair.target];
    const auto upper =
        std::min({defaults.max_bytes, sources, target.len_bits ? 16 * defaults.bytes : sources});
    auto path = std::min(std::bit_floor(upper), limits.max_bytes);
    path = path > defaults.bytes ? path : 0;
    profile.decompose_wrap_bursts = profile.decompose_wrap_bursts || !path || source_wrap > path;
    const auto pack = std::min<std::uint64_t>(profile.data_bits / 8, defaults.bytes);
    const bool aligned = wrap_aligned(plan, index, pair.target);
    for (std::size_t m = 0; m < target.mappings.size(); ++m) {
      const auto &x = target.mappings[m];
      // A mapping of a target flow that no flow of the source reaches is a decode miss.
      const auto reaching = reaching_flows(plan, source.name, target.name, x.flow);
      if (!reaching)
        continue;
      const bool holds = !x.boundaries.empty();
      // A sub-page mapping (its page cut) or one holding another: no WRAP container up to the
      // source's limit crosses a mapping edge; in a page shared with another target or of AXI4
      // sub-page mappings (decoded once at an AXI4 outlet), or a mapping holding another, none up
      // to the largest the source sends (a dedicated outlet decodes every beat), and a packet
      // into another target waits until the earlier packets of its burst are answered.
      const bool sub = target.protocol == Protocol::Axi4 && x.window.size % 4096,
                 cut = x.window.size % 4096 || holds, edge = x.shared || sub || holds;
      // A block holding others keeps its boundary to itself: its window decodes before the rest
      // of the mapping (the smaller first).
      auto add = [&](const AddressWindow &window, std::uint64_t block) {
        const auto own = pair_limits(plan, pair, m, block);
        const auto wrap = !path || !source_wrap ? 1
                          : cut                 ? std::min({path, source_wrap, own.boundary_bytes})
                                                : std::min(path, source_wrap);
        profile.decompose_wrap_bursts =
            profile.decompose_wrap_bursts || (cut && (sub ? largest : source_wrap) > wrap);
        across = across || (edge && largest > wrap);
        (holds ? outer : profile.windows)
            .push_back({window, own.max_bytes, own.boundary_bytes, own.bytes, wrap, pack,
                        plan.flow_group(pair.target, x.flow), std::nullopt,
                        target.read_side_effects, reaching != all_flows(source), target.read_only,
                        aligned, interleave_words(target) > 1 ? *target.read_interleave_bytes : 0U,
                        read_packing_bytes(target, pack), target.protocol == Protocol::AhbV3});
        profile.decompose_wrap_bursts =
            profile.decompose_wrap_bursts || interleave_words(target) > 1;
      };
      std::uint64_t held = 0;
      for (const auto &[block, boundary] : x.boundaries) {
        add(block, boundary);
        held += block.size;
      }
      if (held < x.window.size)
        add(x.window, 0);
      profile.serialize = profile.serialize || x.shared;
      if (implicit
              ? *implicit == std::pair{pair.target, m}
              : pair.target == fallback && (!miss.window.size || x.window.size < miss.window.size))
        miss = (holds ? outer : profile.windows).back();
      if (x.window.size % 4096)
        pages.emplace(x.window.base / 4096, plan.flow_group(pair.target, x.flow));
    }
  }
  // A mapping holding others decodes after them, the smaller first.
  std::ranges::stable_sort(outer, {}, [](const auto &w) { return w.window.size; });
  std::ranges::copy(outer, std::back_inserter(profile.windows));
  // A decode miss is cut as at the implicit_miss mapping or, when the source names its default
  // target, by the limits of a window over the whole space, and answered by the default target.
  if (!miss.window.size)
    miss.group = plan.flow_group(fallback, missed.target_path);
  miss.miss = missed.window;
  miss.exact = false;
  miss.refused = false;
  miss.align_wrap = false;
  miss.read_granule = 0;
  if (source.default_target)
    std::tie(miss.max_bytes, miss.boundary_bytes) = named_miss_limits(plan, index, fallback);
  // The rest of a cut page is a decode miss.
  for (const auto &[page, owner] : pages) {
    auto &w = profile.windows.emplace_back(miss);
    w.window = {page * 4096, 4096};
    w.boundary_bytes = std::min<std::uint64_t>(w.boundary_bytes, 4096);
    profile.serialize = profile.serialize || miss.group != owner;
  }
  // Cutting only at cut page edges keeps every WRAP container within its window's boundary.
  if (!profile.decompose_wrap_bursts && across) {
    profile.decompose_wrap_bursts = true;
    for (auto &w : profile.windows)
      w.wrap_container_limit = std::min(largest, w.boundary_bytes);
    miss.wrap_container_limit = std::min(largest, miss.boundary_bytes);
  }
  if (profile.source_miss)
    profile.fallback = miss;
  miss_drop_flags(source, profile);
  // Behind a packets pipe the packet waits at the request merger (as the pipe already holds it).
  const auto packets_pipe = [](const EndpointPipe &pipe) {
    return pipe.name.ends_with("packets/write") || pipe.name.ends_with("packets/read");
  };
  if (profile.serialize && std::ranges::any_of(source.pipes, packets_pipe))
    std::tie(profile.serialize, profile.merger_holds) = std::pair{false, true};
  return profile;
}

Axi4UpsizeProfile upsize_profile(const NetworkPlan &plan, unsigned index) {
  auto profile = burst_fields<Axi4UpsizeProfile>(plan, "t" + std::to_string(index));
  profile.data_bits = endpoint_word(plan, plan.targets[index].name) * 8;
  profile.response_run_bits = plan.response_run_bits();
  profile.decode_packets = plan.error_codes;
  profile.decode_flag = decode_flag(plan);
  profile.exclusive = plan.exclusive;
  profile.tag_bits = plan.burst_tag_bits;
  profile.max_outstanding = plan.targets[index].outstanding;
  profile.arrival = arrival_block(plan, plan.targets[index]);
  profile.joined = joined_target(plan, plan.targets[index]);
  profile.read_only = plan.targets[index].read_only;
  return profile;
}

// In per-endpoint width mode every reached AXI4 target, and every AHB target wider than a source it
// serves, merges narrow bursts into target words, whatever their AxCACHE.
std::set<std::string> upsize_targets(const NetworkPlan &plan) {
  std::set<std::string> out;
  if (!plan.per_target)
    return out;
  for (const auto &route : plan.exchange.request.routes) {
    const auto protocol = plan.target(route.exit).protocol;
    if (plan.per_target && (protocol == Protocol::Axi4 ||
                            (protocol == Protocol::AhbV3 &&
                             endpoint_word(plan, route.entry) < endpoint_word(plan, route.exit))))
      out.insert(route.exit);
  }
  return out;
}
// The default ID width of an AXI4 target: the widest source ID with bits to count the sources above
// it when several bursts are in flight or the target monitors exclusive access itself.
unsigned whole_target_id_bits(const NetworkPlan &plan, const TargetPlan &target) {
  Axi4OutletProfile outlet;
  outlet.id_bits = plan.burst_id_bits;
  outlet.max_outstanding = target.outstanding > 1 ? 2 : 1;
  outlet.id_sources = reaching_sources(plan, target.name);
  outlet.exclusive_target = target.exclusive;
  outlet.flows = target.flows;
  return axi4_outlet_id_bits(outlet);
}
// The ID bits an AXI4 target must have: those of its flow number plus the owner flags of the
// exclusive accesses a target monitor tells apart.
unsigned least_target_id_bits(const TargetPlan &target) {
  return target.flow_bits() + (target.exclusive.mode == ExclusiveAccess::Target
                                   ? static_cast<unsigned>(target.exclusive.id_flags.size())
                                   : 0U);
}
bool lite_target(const NetworkPlan &plan, const std::string &target) {
  return plan.target(target).protocol == Protocol::Axi4Lite;
}
// Targets served by the shared dedicated outlet engine rather than an AXI4 burst outlet.
bool dedicated_protocol(Protocol protocol) {
  return protocol_caps(protocol).egress && protocol != Protocol::Axi4;
}
// The mode windows of a source: the global windows of the mode-dependent mappings of the targets
// it reaches, by base, adjacent ones of one plane merged.
static void mode_windows(const NetworkPlan &plan, unsigned index, Axi4InitiatorProfile &profile) {
  std::vector<ModeWindow> out;
  for (const auto &pair : plan.pairs)
    for (const auto &m : plan.targets[pair.target].mappings)
      if (pair.source == index && m.plane) {
        ModeWindow w{{m.window.base - (std::uint64_t{m.plane} << plan.plane_shift), m.window.size},
                     0,
                     0,
                     m.plane};
        for (const auto &[flag, value] : m.when) {
          const auto bit =
              1U << (std::ranges::find(plan.address_selectors, flag, &AddressSelector::name) -
                     plan.address_selectors.begin());
          w.care |= bit;
          w.value |= value ? bit : 0U;
        }
        out.push_back(w);
      }
  if (out.empty())
    return;
  std::ranges::sort(out, {}, [](const ModeWindow &w) { return std::pair{w.window.base, w.plane}; });
  auto &windows = profile.mode_windows;
  for (const auto &w : out)
    if (!windows.empty() && windows.back().plane == w.plane &&
        windows.back().window.base + windows.back().window.size == w.window.base)
      windows.back().window.size += w.window.size;
    else
      windows.push_back(w);
  profile.address_selector_bits = static_cast<unsigned>(plan.address_selectors.size());
  profile.plane_shift = plan.plane_shift;
  profile.plane_bits = plan.plane_bits;
}
Axi4InitiatorProfile initiator_profile(const NetworkPlan &plan, unsigned index) {
  const auto &sp = plan.sources[index];
  const auto &source = sp.name;
  Axi4InitiatorProfile profile{
      "s" + std::to_string(index), sp.engine_bits, 32,   plan.burst_id_bits,
      plan.burst_len_bits,         true,           true, plan.source_bits};
  profile.address_bits = sp.address_bits;
  profile.transport_address_bits = plan.burst_address_bits;
  profile.start_address = true;
  profile.uniform_slverr = !plan.error_codes;
  profile.lite = sp.protocol == Protocol::Axi4Lite;
  profile.apb = sp.apb;
  profile.apb_data_bits = sp.apb_bits;
  profile.ahb = sp.ahb;
  profile.axi3 = sp.axi3;
  profile.port_pipes = !sp.ahb && !sp.apb && sp.protocol != Protocol::Axi4Lite;
  // A full AXI4 source admits whole pages; its splitter sends the misses of a cut page on.
  // A window of a target flow reached by some flows of a source of several admits those alone.
  auto &admitted = profile.admitted_windows.emplace();
  std::vector<std::uint64_t> masks;
  for (const auto &pair : plan.pairs)
    if (pair.source == index)
      for (const auto &m : plan.targets[pair.target].mappings) {
        const auto mask = reaching_flows(plan, source, plan.targets[pair.target].name, m.flow);
        if (!mask)
          continue;
        for (const auto &r : m.routes) {
          const AddressWindow page{r.base / 4096 * 4096, 4096};
          if (r.size % 4096 && !profile.lite) {
            profile.crossing = sp.outstanding > 1;
            if (const auto at = std::ranges::find(admitted, page); at == admitted.end()) {
              admitted.push_back(page);
              masks.push_back(mask);
            } else {
              masks[static_cast<std::size_t>(at - admitted.begin())] |= mask;
            }
          } else {
            admitted.push_back(r);
            masks.push_back(mask);
          }
        }
      }
  if (std::ranges::any_of(masks, [&](std::uint64_t m) { return m != all_flows(sp); })) {
    profile.admitted_flows = masks;
    profile.flows = sp.id_ranges;
  }
  // The destinations are the routing windows in target order; a miss takes the default target's
  // first.
  if (const auto missed = miss_route(plan, source)) {
    profile.miss_window = missed->window;
    if (plan.observes_miss_address)
      profile.miss_kept = kept_of_miss(plan, *source_index(plan, source));
    profile.record_bits = plan.burst_record_bits;
    profile.security_bits = plan.burst_security_bits;
    if (sp.outstanding > 1) {
      const auto routes = plan.windows();
      profile.miss_destination =
          static_cast<unsigned>(std::ranges::find(routes, *profile.miss_window) - routes.begin());
    }
  }
  profile.exclusive = plan.exclusive;
  if (!profile.lite && !sp.read_only && !sp.write_only)
    profile.exclusive_windows = exclusive_windows(plan, source);
  // Exclusive windows check locked bursts themselves; a decode miss reaches no target.
  if (sp.packets && !profile.lite)
    for (const auto packets = packet_profile(plan, index); const auto &x : packets.windows)
      profile.lock_windows.push_back(
          {x.window, x.max_bytes, x.boundary_bytes, x.align_bytes,
           !x.miss &&
               std::ranges::none_of(profile.exclusive_windows, [&](const ExclusiveWindow &e) {
                 return x.window.base - e.window.base < e.window.size;
               })});
  profile.late_id_binding = sp.qos.picks() && sp.pooled && sp.outstanding > 1 &&
                            std::ranges::any_of(sp.pipes, [](const auto &pipe) {
                              return pipe.name.ends_with("/pipe/packets");
                            });
  profile.tag_bits = plan.burst_tag_bits;
  profile.user_bits = plan.burst_user_bits;
  profile.response_user_bits = plan.burst_response_user_bits;
  profile.ahb5 = sp.ahb5;
  profile.hmaster_bits = sp.hmaster_bits;
  profile.hprot_bits = sp.hprot_bits;
  profile.hnonsec = sp.hnonsec;
  profile.ahb5_metadata_bits = plan.ahb5 ? 8 + plan.hmaster_bits : 0;
  profile.user_port_bits = sp.user_bits;
  profile.response_user_port_bits = sp.response_user_bits;
  profile.user_flags = sp.user_flags;
  profile.security_flags = sp.security_flags;
  profile.level_bits = plan.burst_level_bits;
  profile.axqos_map = sp.qos.axqos_map;
  profile.source_lock = sp.packets && (sp.split_narrow || exact_packets(plan, index));
  profile.address_hints = sp.packets && !profile.lite && profile.miss_window;
  mode_windows(plan, index, profile);
  if (sp.outstanding > 1) {
    // The issue-order destination decode uses the request routing
    // windows; an unadmitted address takes the default target's first.
    profile.max_outstanding = sp.engine_slots;
    profile.bound_ids = sp.outstanding_ids;
    profile.destinations = plan.windows();
    const auto reached = reached_windows_of(plan, index);
    for (unsigned k = 0; k < profile.destinations.size(); ++k)
      if (const auto &w = profile.destinations[k]; std::ranges::find(reached, w) == reached.end() &&
                                                   std::ranges::any_of(reached, [&](const auto &x) {
                                                     return w.base >= x.base &&
                                                            w.base + w.size <= x.base + x.size;
                                                   }))
        profile.hidden_destinations.push_back(k);
  }
  return profile;
}
// The mappings an outlet decodes, in base order.
static std::vector<AddressMapping> outlet_mappings(const TargetPlan &tp) {
  std::vector<AddressMapping> out;
  for (const auto &m : tp.mappings)
    std::ranges::transform(m.routes, std::back_inserter(out), [&](const AddressWindow &r) {
      return AddressMapping{.base = r.base,
                            .size = r.size,
                            .local_address = m.local_base + (r.base - m.window.base),
                            .flow = m.flow};
    });
  std::ranges::sort(out, {}, &AddressMapping::base);
  return out;
}
// The aligned block holding a target's routing windows widened to whole pages, with the page
// above them when `above`; none when that reaches the top of the global address space.
static AddressBlock page_block(const NetworkPlan &plan, const TargetPlan &tp, bool above) {
  std::uint64_t lo = ~std::uint64_t{0}, hi = 0;
  for (const auto &r : tp.routes()) {
    lo = std::min(lo, r.base / 4096 * 4096);
    hi = std::max(hi, (r.base + r.size + 4095) / 4096 * 4096 + (above ? 4096 : 0));
  }
  if (hi == 0 || hi > std::uint64_t{1} << plan.burst_address_bits)
    return {};
  const auto keep = std::max(12U, static_cast<unsigned>(std::bit_width(lo ^ (hi - 1))));
  return {lo >> keep << keep, keep};
}
// The arrival block of a target (AddressBlock): its page block with the page above, into which a
// burst moved to the base of a default target's sub-page window may walk (Logic::moved_to_miss);
// none when it is the whole address space. Every outlet keeps its addresses inside it.
AddressBlock arrival_block(const NetworkPlan &plan, const TargetPlan &tp) {
  const auto block = page_block(plan, tp, true);
  return block.keep < plan.burst_address_bits ? block : AddressBlock{};
}
unsigned arrival_bits(const NetworkPlan &plan, const TargetPlan &tp) {
  const auto block = arrival_block(plan, tp);
  return block.keep ? block.keep : plan.burst_address_bits;
}
AddressBlock transport_span(const NetworkPlan &plan, const TargetPlan &tp) {
  return page_block(plan, tp, tp.fallback);
}
BurstProfile endpoint_burst(const NetworkPlan &plan) {
  BurstProfile burst{plan.burst_id_bits,      plan.burst_len_bits,  true,
                     plan.burst_address_bits, plan.occupancy_bits,  plan.exclusive,
                     plan.burst_tag_bits,     plan.burst_user_bits, plan.burst_response_user_bits};
  burst.level_bits = plan.burst_level_bits;
  burst.record_bits = plan.burst_record_bits;
  burst.security_bits = plan.burst_security_bits;
  return burst;
}
bool interleaved_target(const NetworkPlan &, const TargetPlan &target) {
  if (target.read_interleave_bytes)
    return *target.read_interleave_bytes != 0;
  const auto bytes = target.data_bits / 8;
  const bool multi_beat = (std::uint64_t{1} << target.len_bits.value_or(0)) * bytes != bytes;
  const bool contexts = target.flows > 1 || target.id_bits.value_or(0) != 0 || !target.read_only;
  return target.protocol == Protocol::Axi4 && multi_beat && !target.write_only && contexts;
}
bool reads_granule_target(const NetworkPlan &plan, unsigned index) {
  for (unsigned t = 0; t < plan.targets.size(); ++t)
    if (interleave_words(plan.targets[t]) > 1 && plan.route_index.reaching[index][t].reached)
      return true;
  return false;
}
bool decode_flag(const NetworkPlan &plan) {
  return !plan.fault_monitors.empty() ||
         std::ranges::any_of(plan.targets, [](const TargetPlan &t) { return t.fixed_length; });
}

bool joins_directly(const TargetPlan &target) {
  if (target.read_interleave_bytes)
    return *target.read_interleave_bytes == 0U && !target.write_only;
  // A read-only target with no ID has nothing to interleave: it answers as with a size of 0.
  const auto bytes = target.data_bits / 8;
  const bool multi_beat = (std::uint64_t{1} << target.len_bits.value_or(0)) * bytes != bytes;
  return target.protocol == Protocol::Axi4 && multi_beat && target.read_only && target.flows == 1 &&
         target.id_bits.value_or(0) == 0;
}
bool joined_target(const NetworkPlan &plan, const TargetPlan &target) {
  return interleaved_target(plan, target) || joins_directly(target);
}
unsigned interleave_words(const TargetPlan &target) {
  return std::max(1U, target.read_interleave_bytes.value_or(0) / (target.data_bits / 8));
}
BurstProfile transport_burst(const NetworkPlan &plan) {
  auto burst = endpoint_burst(plan);
  burst.level_bits = burst.response_level_bits = plan.transport_level_bits();
  burst.raise = plan.regulated();
  burst.address_bits = plan.transport_address_bits;
  burst.start_offset_bits = 12;
  burst.target_bits = plan.target_bits;
  burst.path_bits = plan.burst_path_bits;
  burst.segments = plan.response_segments;
  burst.response_runs = plan.response_runs;
  burst.decode_packets = plan.error_codes;
  burst.decode_flag = decode_flag(plan);
  return burst;
}
// The fields both outlet profiles take from their target.
template <typename Profile>
static Profile target_fields(Profile profile, const NetworkPlan &plan, const TargetPlan &tp) {
  profile.attributes = tp.attributes;
  profile.read_only = tp.read_only;
  profile.write_only = tp.write_only;
  profile.error_codes = plan.error_codes;
  if (tp.timeout) {
    profile.timeout_cycles = tp.timeout->cycles;
    profile.timeout_completion = tp.timeout->completion;
  }
  profile.exclusive = plan.exclusive;
  profile.tag_bits = plan.burst_tag_bits;
  profile.user_bits = plan.burst_user_bits;
  profile.response_user_bits = plan.burst_response_user_bits;
  profile.user = tp.user;
  profile.response_user_port_bits = tp.response_user_bits;
  profile.exclusive_target = tp.exclusive;
  profile.arrival = arrival_block(plan, tp);
  return profile;
}
// The shared-bus outlet (APB, AXI4-Lite, AHB) of one target.
BurstOutletProfile outlet_profile(const NetworkPlan &plan, unsigned index) {
  const auto &tp = plan.targets[index];
  auto profile =
      target_fields(BurstOutletProfile{"t" + std::to_string(index), plan.burst_id_bits,
                                       plan.burst_len_bits, plan.source_bits, outlet_mappings(tp),
                                       tp.local_bits, plan.burst_address_bits},
                    plan, tp);
  profile.response_run_bits = plan.response_run_bits();
  if (protocol_caps(tp.protocol).apb) {
    profile.dialect = tp.protocol;
    profile.apb_back_to_back = tp.back_to_back;
    profile.apb_data_bits = tp.apb_bits;
  } else
    profile.data_bits = endpoint_word(plan, tp.name) * 8;
  profile.fixed_single = tp.fixed_single;
  profile.complete_write_bursts = tp.complete_writes;
  profile.undefined_length_bursts = !tp.fixed_length;
  profile.hresp_width = tp.hresp_bits;
  profile.ahb5 = tp.ahb5;
  profile.hmaster_bits = tp.hmaster_bits;
  profile.hprot_bits = tp.hprot_bits;
  profile.hnonsec = tp.hnonsec;
  profile.ahb5_metadata_bits = plan.ahb5 ? 8 + plan.hmaster_bits : 0;
  if (tp.protocol == Protocol::Axi4Lite && tp.outstanding > 1)
    profile.max_outstanding = tp.outstanding;
  profile.flows = tp.flows;
  return profile;
}
ServiceOutletProfile service_outlet_profile(const NetworkPlan &plan, unsigned index) {
  const auto &tp = plan.targets[index];
  ServiceOutletProfile profile{"t" + std::to_string(index)};
  profile.id_bits = plan.burst_id_bits;
  profile.len_bits = plan.burst_len_bits;
  profile.source_bits = plan.source_bits;
  profile.tag_bits = plan.burst_tag_bits;
  profile.user_bits = plan.burst_user_bits;
  profile.response_user_bits = plan.burst_response_user_bits;
  profile.exclusive = plan.exclusive;
  profile.transport_address_bits = plan.burst_address_bits;
  profile.arrival = arrival_block(plan, tp);
  profile.response_run_bits = plan.response_run_bits();
  profile.window_bits = static_cast<unsigned>(std::bit_width(tp.mappings.front().window.size - 1));
  const auto &mapping = tp.mappings.front();
  profile.mapping = {mapping.window.base, mapping.window.size, mapping.local_base};
  const auto &banks = tp.service->banks;
  profile.bank_bits = banks.front().plan.address_bits;
  profile.route = banks.front().plan.route;
  if (banks.size() > 1 || banks.front().base != 0)
    std::ranges::transform(banks, std::back_inserter(profile.places), [](const ServiceBank &bank) {
      return ServiceOutletProfile::Place{bank.base, bank.plan.route, bank.branch};
    });
  return profile;
}
Axi4OutletProfile axi4_outlet_profile(const NetworkPlan &plan, unsigned index) {
  const auto &tp = plan.targets[index];
  auto profile =
      target_fields(Axi4OutletProfile{"t" + std::to_string(index), endpoint_word(plan, tp.name) * 8,
                                      plan.burst_id_bits, plan.burst_len_bits, plan.source_bits,
                                      true, outlet_mappings(tp)},
                    plan, tp);
  profile.decode_flag = decode_flag(plan);
  profile.address_bits = tp.local_bits;
  profile.transport_address_bits = plan.burst_address_bits;
  profile.port_id_bits = tp.id_bits.value();
  profile.port_len_bits = tp.len_bits.value();
  profile.max_outstanding = tp.outstanding;
  profile.merged = upsize_targets(plan).contains(tp.name);
  profile.admitted = !profile.merged && std::ranges::any_of(tp.pipes, [](const EndpointPipe &e) {
    return e.name.ends_with("/pipe/dispatch");
  });
  profile.interleave = joined_target(plan, tp);
  profile.interleave_beats = interleave_words(tp);
  profile.response_direct = joins_directly(tp);
  profile.response_run_bits = plan.response_run_bits();
  profile.one_head = profile.merged && profile.interleave && tp.outstanding == 1 &&
                     std::ranges::none_of(tp.pipes, [](const EndpointPipe &e) {
                       return e.name.ends_with("/pipe/dispatch");
                     });
  profile.merger_hints = tp.outstanding > 1 && !tp.write_only && !profile.interleave;
  profile.axi3 = tp.axi3;
  profile.flows = tp.flows;
  profile.align_wrap = aligns_wrap_reads(plan, index);
  profile.id_sources = reaching_sources(plan, tp.name);
  // Transaction identities contain no mapping-block index.
  const auto &ids = plan.route_index.target_ids;
  profile.id_route.direction_bits = ids.direction_bits;
  for (unsigned g = 0; g < tp.flows; ++g)
    profile.id_route.target_paths.push_back(
        std::uint64_t{(ids.targets.contains(tp.name) ? ids.targets.at(tp.name) : 0U) + g}
        << ids.shift("target_path"));
  for (const auto si : profile.id_sources) {
    const auto &source = plan.sources[si];
    TargetIdRoute::Source entry;
    entry.direction = ids.direction_bits && !source.apb;
    const auto first = ids.initiators.contains(source.name) ? ids.initiators.at(source.name) : 0U;
    for (unsigned f = 0; f < std::max<std::size_t>(1, source.flows.size()); ++f) {
      entry.flows.push_back(std::uint64_t{first + f} << ids.shift("source_path"));
    }
    entry.ranges = source.id_ranges;
    profile.id_route.sources.push_back(std::move(entry));
  }
  return profile;
}
} // namespace nocloom::detail
