#pragma once
#include "exclusive.hpp"
#include "logic.hpp"

namespace nocloom::detail {
// Exclusive access monitor of a target outlet (exclusive: network, AXI
// A7.2.3). It sits where every access to the target passes, so it observes
// every write the target receives.
//
// A reservation records the key of an exclusive read, the value of the owner flags of its user
// field (Target::exclusive_id_flags), and its region: the access rounded out to the granule, base
// aligned to its size. One key holds one
// reservation: a new exclusive read of the key replaces it (A7.2.1). A full
// table replaces the slot a free-running 5-bit LFSR selects, so two keys that
// keep evicting each other do not lock into one order.
//
// Events, decided at a packet head (descriptor capture):
// - exclusive read: replaces the key's reservation with the new region,
//   whatever the read answers, unless a write that reaches the target is in
//   flight over it (from its descriptor capture until its response leaves
//   the outlet; a failed exclusive write or a write answered locally does
//   not count), in which case the read still answers EXOKAY but leaves the
//   monitor unchanged: it records nothing, so the following exclusive write
//   fails, and the key keeps its earlier reservation;
// - exclusive write: succeeds when the key holds a reservation of exactly its
//   region; a success reaches the target, a failure does not;
// - any write that reaches the target (a normal write or a successful
//   exclusive write): drops every reservation its range overlaps, its own
//   included.
// The common reset drops every reservation.
struct ExclusiveMonitorProfile {
  unsigned monitors = 1;      // 1..16
  unsigned granule = 1;       // bytes, power of two 1..128
  unsigned address_bits = 32; // transport address width
  // Bits of the user field forming the key, the low bit of the key first.
  std::vector<unsigned> key_flags{};
};
// The byte range [low, high) of a packet from its descriptor fields address
// (its first beat container base), length, size and burst: the WRAP
// container, the FIXED beat, or the INCR span. One bit wider than the
// address so the top of the space does not wrap.
struct ExclusiveRange {
  ValueId low, high;
};
ExclusiveRange packet_range(logic::Logic &l, ValueId address, ValueId length, ValueId size,
                            ValueId burst);

class ExclusiveMonitor {
public:
  ExclusiveMonitor(logic::Logic &l, std::string prefix, const ExclusiveMonitorProfile &p);
  // The reservation key of a request from its user field.
  ValueId key(const std::map<std::string, ValueId> &request) const;
  // An exclusive access's region (base, log2 bytes) from its descriptor.
  std::pair<ValueId, ValueId> region(ValueId address, ValueId length, ValueId size) const;
  // True when the owner holds a reservation of exactly this region.
  ValueId holds(ValueId owner, std::pair<ValueId, ValueId> bounds) const;
  // True when [low, high) overlaps the region.
  ValueId overlaps(ExclusiveRange range, std::pair<ValueId, ValueId> bounds) const;
  // Applies one cycle's events: an exclusive read that records its key's
  // reservation at its request (record), whatever it answers, and a write
  // that drops every reservation its range overlaps. A recorded slot keeps
  // its new reservation over a drop.
  void update(ValueId record, ValueId read_key, std::pair<ValueId, ValueId> read_region,
              ValueId write, ExclusiveRange write_range);
  // Every state word of the monitor, for the outlet's storage records.
  static std::vector<std::string> state_names(const std::string &prefix,
                                              const ExclusiveMonitorProfile &p);

private:
  logic::Logic &l_;
  std::string prefix_;
  ExclusiveMonitorProfile p_;
  unsigned key_bits_ = 0;
  struct Slot {
    ValueId valid, key, base, log;
  };
  std::vector<Slot> slots_;
  ValueId lfsr_;
};
} // namespace nocloom::detail
