#pragma once
#include "cross_domain.hpp"
#include "hardware_builder.hpp"
#include <cstdint>
#include <optional>
#include <utility>

namespace nocloom::detail {
inline constexpr unsigned async_fifo_min_depth = 2;
inline constexpr unsigned async_fifo_max_depth = 10;
// Depth used when either clock period is unknown.
inline constexpr unsigned async_fifo_fallback_depth = 6;
inline constexpr unsigned async_fifo_default_stages = 2;
inline constexpr std::uint64_t crossing_min_period_ps = 100;
inline constexpr std::uint64_t crossing_max_period_ps = 1000000000;

enum class CrossingDepthSource { Formula, Fallback, Explicit };
struct CrossingDepth {
  unsigned depth = 0;
  CrossingDepthSource source = CrossingDepthSource::Fallback;
  bool operator==(const CrossingDepth &) const = default;
};
// Depth for one directed crossing from sender a to receiver b whose count
// synchronizers both have n stages. With both periods known it covers the slot
// turnaround loop measured in slow-clock periods:
//   ceil((n + 1) * (Ta + Tb) / max(Ta, Tb))
// Without both periods the depth is async_fifo_fallback_depth. Integer arithmetic.
Result<CrossingDepth> default_crossing_depth(unsigned stages,
                                             std::optional<std::uint64_t> sender_period_ps,
                                             std::optional<std::uint64_t> receiver_period_ps);
// An explicit depth overrides the default after its range check.
Result<CrossingDepth> resolve_crossing_depth(std::optional<unsigned> requested, unsigned stages,
                                             std::optional<std::uint64_t> sender_period_ps,
                                             std::optional<std::uint64_t> receiver_period_ps);

struct FifoDomain {
  DomainPlan plan;
  ValueId clock{}, reset{};
};
struct FifoField {
  std::string name;
  unsigned width = 0;
  ValueId input{};
  std::vector<ValueId> storage; // one sender-clocked slot per FIFO entry; reset iff reset_all
  ValueId selected{};           // storage[read_index], combinational in the receiver
  ValueId received{};           // receiver forward register; reset iff reset_all
  ValueId output{};             // received qualified by out_valid
};
// One side's reset handshake: a clear token drains through `stages` local stages before announce.
// Announce rises once the acknowledge (the
// other side's copy of announce) reads low, on once it reads high; while enable (the copy of the
// other side's announce & on) is low the side clears its index and count and passes nothing.
struct FifoHandshake {
  ValueId announce{}, on{}, wait{}, enable{};
  std::vector<ValueId> peer_sync{}, ack_sync{};
};
// One-way Gray-count asynchronous FIFO. Domain 0 sends and domain 1 receives.
// Payload slots live in the sender clock and are selected by the receiver read
// index into a receiver forward register; only the count registers cross through
// synchronizer chains. Each side's reset clears the control registers of its
// clock, its count synchronizer chain included. Asserting both resets together cancels any
// queued words; either may be released first. With the reset handshake either side may also be
// reset alone, and the counts have no reset pin. Payload registers have a reset iff reset_all;
// every payload output reads zero while output_valid is low.
struct AsyncFifo {
  HardwareIR ir;
  std::array<FifoDomain, 2> domains;
  unsigned depth = 0, count_bits = 0, index_bits = 0, stages = 0;
  bool reset_handshake = false, reset_all = false;
  // Sender side.
  ValueId valid, ready, write, full, write_index, write_count;
  std::vector<ValueId> read_count_sync;
  // Receiver side; output_valid is out_valid, qualified by the receiver enable with the handshake.
  ValueId out_ready{}, out_valid{}, output_valid{}, nonempty{}, read{}, read_index{}, read_count{};
  std::vector<ValueId> write_count_sync;
  std::array<FifoHandshake, 2> handshake{};
  std::vector<FifoField> fields;
  std::vector<std::pair<std::string, unsigned>> layout;
};
// Every synchronizer chain of the FIFO has `stages` registers, 2..4.
Result<AsyncFifo> lower_async_fifo(const DomainPlan &from, const DomainPlan &to, unsigned depth,
                                   unsigned stages,
                                   const std::vector<std::pair<std::string, unsigned>> &fields,
                                   bool reset_handshake = false, bool reset_all = false);
Status verify_async_fifo(const AsyncFifo &fifo);
unsigned async_fifo_count_bits(unsigned depth);
unsigned async_fifo_index_bits(unsigned depth);
} // namespace nocloom::detail
