#pragma once
#include "component.hpp"
#include <cstdint>
#include <nocloom/definition.hpp>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace nocloom::detail {
// Source-side packet splitter of every AXI4 source; both faces keep the source layout and carry the
// burst's start address. Per window (the first one holding a packet's start), an INCR packet ends
// at floor(start, align) + max_bytes or the next boundary_bytes boundary. FIXED, exclusive (lock)
// and unsplit WRAP bursts, and starts outside every window, stay one packet. A narrow WRAP whose
// container fits one source word folds into one INCR beat of the container's size at its base.
// decompose_wrap_bursts: another WRAP runs as INCR when it starts at its container base, or as two
// INCR runs (start to top, base to start) when its container exceeds the window's
// wrap_container_limit. split_narrow: a narrow INCR with AxCACHE[1] and AxLOCK clear goes one
// packet per beat; AxLOCK comes on the "hint" face from the initiator descriptor, since the Weft
// lock field is clear at targets without exclusive support. Every packet of an INCR (or split WRAP
// run) narrower than the window's packing word, or of one beat of it, goes out in packing words:
// AxADDR kept, AxLEN the words covered, AxSIZE the packing word for several words or the smallest
// size covering the bytes of one; write beats merge into a word by strobe, and each returned word
// answers every source beat it holds. Up to piece_count::kInFlight packets per direction follow
// each other without waiting for responses (a narrow-split burst's pieces are exempt: the shared
// pool counts each of them); they reach one target in order, intermediate B responses merge
// (network miss first) and the source sees one B and one R stream. Above capacity one, a single
// plain packet passes with its own tag; a burst needing cutting, splitting, folding or packing of
// several beats waits only for a vacant reassembly context (a write: and packet room), bursts and
// their responses interleave, and a register replays a read word answering several beats.
// w/r.burst_final_packet mark a valid down beat or request of the burst's last packet. A W beat (or
// the word it completes) leaves in the cycle it is offered and is taken only then, so the up face's
// W ready follows the down face's; a beat that only adds to an open word is taken at once. A read
// burst's first packet leaves in its own cycle (never after a ready); one not taken stays. Exact
// packets: from the burst start, each packet is the largest naturally aligned power-of-two block of
// the remaining bytes (at most the window's packet limit and boundary). A block smaller than the
// beat (only inside an unaligned first beat) is one beat of the block's size at its own address: a
// write sends the first beat once per block with the block's strobes, a read gathers the blocks'
// responses into one source beat. A returned packed word answers only the beats of its block.
struct Axi4PacketWindow {
  AddressWindow window;             // the target's own window (sub-page sizes kept)
  std::uint64_t max_bytes = 0;      // packet length limit, a power of two
  std::uint64_t boundary_bytes = 0; // packet boundary, a power of two dividing base and size
  std::uint64_t align_bytes = 0;    // max(source bytes, target bytes)
  // Largest WRAP container kept whole under decompose_wrap_bursts, a power of two; 1 cuts every
  // unaligned WRAP.
  std::uint64_t wrap_container_limit = 1;
  // Packing word: min(source bytes, target bytes), 4 at least.
  std::uint64_t pack_bytes = 4;
  // The target the window routes to.
  unsigned group = 0;
  // A decode miss (listed after the windows it holds): its packet carries the error flag with its
  // address moved into this window (keeping the page offset in a window of whole pages); each
  // write beat leaves at once as one four-byte fragment, the first one the packet's head.
  std::optional<AddressWindow> miss;
  // The target has read side effects: an INCR with AxCACHE[1] and AxLOCK clear whose start this
  // window holds goes out as naturally aligned power-of-two blocks (exact packets).
  bool exact = false;
  // Only some flows of the source reach the window (connectivity by flow pair): a request the
  // source moved (error flag set) whose own address it holds is a decode miss as beyond every
  // window.
  bool partial = false;
  // The target refuses writes (a read-only target): a write packet starting here goes out as its
  // first beat alone with AxLEN 0, the beats after it dropped like a decode miss's (miss_drop).
  bool refused = false;
  // The target aligns WRAP reads (an AXI4 target as wide as the source and the packing word,
  // Axi4OutletProfile::align_wrap): a read WRAP narrower than the packing word over more than one
  // word, neither folded, cut nor exclusive, goes out in packing words as a WRAP over its
  // container, AxLEN the container's words less one, AxADDR its start word and the start address
  // the source AxADDR; each returned word answers the source beats it holds, a start word answered
  // twice when the burst wraps back into it.
  bool align_wrap = false;
  // The target's multi-word read interleave granule in bytes, a power of two (else zero): a read
  // starting inside such a block ends its packet at the block's top, and a WRAP read over more than
  // a block starting inside one is cut.
  std::uint64_t read_granule = 0;
  // Physical read packing word, 1..pack_bytes. Zero uses pack_bytes.
  std::uint64_t read_pack_bytes = 0;
  // Pack ordinary reads only from an aligned word, avoiding a split physical suffix.
  bool read_pack_aligned = false;
};
struct Axi4PacketizerProfile : ComponentProfileBase {
  unsigned data_bits = 32; // source width: 32..1024
  bool write = true;       // false: a read-only source, without the write faces
  bool split_narrow = false;
  bool decompose_wrap_bursts = false;
  std::vector<Axi4PacketWindow> windows;
  // Packets of one burst may reach different targets (windows of different groups): a packet
  // whose group differs from the previous packet's waits until every earlier packet of its
  // burst is answered, so responses keep packet order; other bursts in flight do not hold it.
  bool serialize = false;
  // In place of serialize when a packets pipe follows the splitter: the splitter sends such a
  // packet on at once and the source's request merger, behind that pipe, holds it instead.
  bool merger_holds = false;
  // Weft exclusive fields (message.hpp contract point 11).
  bool exclusive = false;
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13)
  unsigned response_user_bits = 0;
  unsigned level_bits = 0; // request scheduling level (message.hpp point 15)
  // Above one, single-packet bursts pass independently; splitting, folding or
  // packing takes a reassembly context of its own until done.
  unsigned max_outstanding = 1;
  // The source's admission miss window (Axi4InitiatorProfile::miss_window). With it the splitter
  // takes the source's own addresses and write AxLEN from the hint face (write_start,
  // write_address, write_length, read_start) in place of the moved ones and the source sends
  // every beat of a missed write burst.
  std::optional<AddressWindow> source_miss;
  // Address bits a moved request keeps for an error logger, in place of its page offset.
  std::optional<std::uint64_t> miss_kept_bits;
  unsigned record_bits = 0;   // BurstProfile::record_bits
  unsigned security_bits = 0; // BurstProfile::security_bits
  // With source_miss: the decode miss of every address no window holds (its window field unused),
  // a miss window with its own limits. A request the source moved (error flag set) plans its
  // packets from the source's own addresses through it, each packet a miss as above.
  std::optional<Axi4PacketWindow> fallback;
  // Hand each down packet's plan fields to the width cut behind on the hint face
  // (Axi4WidthCutProfile::write_hints, read_hints).
  bool packet_hints = false;
  // A source with decode misses or refused writes: its network entry passes a write miss (or
  // refused) packet's head as the whole packet and drops the fragments after it, as the default
  // target would take the head before the beats (without it the splitter drops them itself:
  // behind a width conversion when a target refuses writes); while they are dropped (the hint
  // face's write_dropping, or w.swallow), w.miss_hold asks the source to hold a B of a context
  // with one packet in flight (several bursts: every B) ahead of any width conversion, so the
  // burst's B never comes before its beats have left.
  bool miss_drop = false;
  // With miss_drop: request stages lie between the splitter and the network entry, so a miss head
  // reaches the entry later; at capacity one its packet counts as the hint face's
  // write_miss_entered marks it passing there.
  bool miss_lag = false;
};

Result<NodeHardware> lower_axi4_packetizer(const Axi4PacketizerProfile &profile);
// Write ("w.") and read ("r.") state bits of the lowered splitter, for budgets.
std::pair<unsigned, unsigned> axi4_packetizer_state_bits(const Axi4PacketizerProfile &profile);
} // namespace nocloom::detail
