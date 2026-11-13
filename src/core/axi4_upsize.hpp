#pragma once
#include "component.hpp"
#include <nocloom/definition.hpp>
#include <string>
#include <utility>

namespace nocloom::detail {
// Target-side merge for bursts narrower than the target.
// Both faces keep the target's packet layout
// (beats sit in their target lanes). Per burst, whatever its AxCACHE:
// - FIXED and WRAP bursts whose AxSIZE is the full target width pass unchanged.
// - INCR becomes one target burst: AxADDR kept, AxLEN = target words - 1,
//   AxSIZE the full width for several words or the smallest size covering the
//   bytes of a single word. Source beats merge into target words by lane; a
//   word goes out when the next beat leaves it or at the last beat.
// - WRAP whose container fits one word becomes one full-width INCR beat at the
//   container base. A larger container becomes a full-width target WRAP; a
//   write whose start is not word aligned starts one word later and sends the
//   start word last, merged with the beats that wrap back into it.
// Read responses fan each target word back out to the source beats it holds,
// with the word's result; a failed word reads as zero. K packet contexts per
// direction retain original descriptors until upstream completion. With K > 1
// a read enters only as it passes on (its request never waits here). Joined, each
// read keeps its own place, and a target word answers the oldest read of the word's
// {source, AxID}, whatever read the previous word belonged to. Not joined (no ID to
// tell reads apart, or single words), a shared gather serves the oldest open read.
// Writes match the oldest retained descriptor of the returning source, ID and tag.
// A legal outlet returns B only after accepting that packet; no early-B detector is
// added here. K=1 is the single-descriptor path; joined changes
// its read side (below): nothing waits here, a read enters as it passes on.
// A request or target word reaching an empty stage passes in its own cycle;
// whatever is not taken waits in the stages. A write word is never stored
// once complete: the beat that completes it is taken in the cycle the outlet
// takes the word, so only the word's open part waits here.
// The response steering tag (tag_bits) rides every request beat to the
// outlet; B passes the outlet's echo and merged R beats carry the tag latched
// with the read descriptor, so bursts of a multi-burst source keep their slot.
struct Axi4UpsizeProfile : ComponentProfileBase {
  unsigned data_bits = 64; // target width: 32, 64, 128, 256, 512 or 1024
  // The address bits of the widest response word a run gathers to (message.hpp point 18): the merge
  // marks where a run ends; zero without runs.
  unsigned response_run_bits = 0;
  // A tagged local miss may arrive as one word (decode_packet.hpp): it leaves as one, its length in
  // source beats, ending the read's fan-out.
  bool decode_packets = false, decode_flag = false;
  // Weft exclusive fields (message.hpp contract point 11).
  bool exclusive = false;
  // Weft response steering tag width (message.hpp point 12); zero omits it.
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13)
  unsigned data_user_bits_per_byte = 0;
  unsigned response_user_bits = 0;
  // Accepted packets retained until their final upstream response transfers.
  unsigned max_outstanding = 1;
  // Arrival block of the merge's global addresses (AddressBlock).
  AddressBlock arrival{};
  // An outlet with interleaved read data (Axi4OutletProfile::interleave): the outlet keeps a target
  // word in its response stage until the word's last source beat leaves here, so the merge takes a
  // word as that beat leaves and keeps no word of its own (as a serializer behind one response
  // register). The hint output word_end marks a beat leaving its word (a segment ends only there).
  // With several bursts each read descriptor owns its start-word storage through its final
  // accepted response. Stored beats leave after that read's last target word.
  bool joined = false;
  // Joined at a read-only target: writes never merge, every beat passes on to the outlet, which
  // answers it locally.
  bool read_only = false;
};

Result<NodeHardware> lower_axi4_upsize(const Axi4UpsizeProfile &profile);
// Write ("w.") and read ("r.") state bits of the lowered merge, for budgets.
std::pair<unsigned, unsigned> axi4_upsize_state_bits(const Axi4UpsizeProfile &profile);
} // namespace nocloom::detail
