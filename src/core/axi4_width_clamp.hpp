#pragma once
#include "component.hpp"
#include <nocloom/definition.hpp>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace nocloom::detail {
// Wide-to-narrow burst cutter shared by the per-endpoint width clamp and
// fixed-width component tests. The up faces carry the source's packet layout;
// the down faces carry down_bits of data (the source width for the clamp, 32
// bits for a fixed-width cut). Each burst takes a target word width c from the
// routing window that holds its start address (fixed_width overrides the
// table; a miss keeps the source width). A burst no wider than c passes
// unchanged (a fixed-width cut selects the addressed 32-bit lane). A wider burst
// becomes target bursts of 2^c-byte beats in source byte order:
// - a WRAP of 2, 4, 8 or 16 target words stays one WRAP over its container;
// - a longer WRAP is cut into INCR pieces, from the start to the container
//   top and then from the container base back to the start (wrap order);
// - an INCR covers the target words from the start word to the burst end;
// - every INCR piece holds at most 2^len_bits target words. Sources never
//   send an INCR that crosses 4 KiB, so no piece crosses it either.
// Pieces follow each other without waiting for responses: the next write piece's first word follows
// the previous piece's last word, and the next read piece's request follows the previous one's
// acceptance, with at most piece_count::kInFlight pieces per direction whose response has not come
// back. All pieces of a burst reach one target in order (piece_count.hpp), so responses come back
// in piece order. The next burst may follow at once (bounded packets arrive as bursts): its write
// words follow the previous burst's, and its read requests follow once the previous burst's
// requests are out, its words gathered after the previous burst's. Intermediate B responses are
// absorbed whatever the request side does and merged in arrival order (network miss before target
// error); the B of a burst's last piece carries the merge, so the source sees one B per burst. Read
// response words are taken whatever the request side does. Read words are gathered back into source
// beats (a beat takes the worst result of its words, and with zero_failed_beats zero data when any
// failed); RLAST marks the last source beat. Nothing waits here: a source write beat is taken with
// its last target word (a beat passed whole as it leaves), and the target word that completes a
// read beat waits on the down face for the up face's ready, the other words being taken at once. A
// FIXED burst is never cut: one wider than c that starts in a fixed_reject window completes locally
// with SLVERR and no target request (write data is accepted and dropped, one B after the last W; a
// read returns AxLEN+1 zero beats). An exclusive burst (lock) is never cut either: one that would
// need more than one piece completes the same way, as does a beat above the source width. A local
// completion waits until every earlier burst's responses have come back. With max_outstanding > 1,
// a packet needing at most one target burst may overlap other packets. Each tag keeps packet_depth
// ordered descriptors; different tags may return in either order. A multi-piece or locally rejected
// packet drains earlier contexts and excludes later ones through final source delivery, retaining
// the four-piece pipeline internally. Burst tag slots and packet contexts are independent of that
// piece credit count. Common reset; a B or R word that comes back while no burst is in flight
// (after a reset of this side alone) is dropped.
struct Axi4WidthCutProfile : ComponentProfileBase {
  std::string module_prefix = "nocloom_axi4_width_clamp_";
  unsigned data_bits = 256; // source width: 64, 128, 256, 512 or 1024
  unsigned down_bits = 256; // down face data width: data_bits or 32
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13), passed through
  unsigned data_user_bits_per_byte = 0;
  unsigned response_user_bits = 0;
  // Routing window and the log2 byte width of its target (2..log2(data/8)).
  std::vector<std::pair<AddressWindow, unsigned>> windows;
  // Every burst at this log2 byte width instead of the window table.
  std::optional<unsigned> fixed_width;
  // Windows of targets whose AxLEN is shorter than len_bits, with its width: a piece stays within
  // it.
  std::vector<std::pair<AddressWindow, unsigned>> piece_len_bits;
  // Own windows of the targets: a FIXED burst wider than its target that starts in one completes
  // locally with SLVERR. The window decode comes first, so a start outside them (a sub-page
  // remainder or an unmapped address) is cut and answers DECERR per word downstream without access.
  std::vector<AddressWindow> fixed_reject;
  bool zero_failed_beats = false;
  // Responses are runs (message.hpp point 18): the down face's count field holds the reach.
  bool response_runs = false;
  // A tagged local miss may arrive as one word (decode_packet.hpp): its target words become source
  // beats at once, and it leaves as one word still.
  bool decode_packets = false, decode_flag = false;
  // Weft exclusive fields (message.hpp contract point 11).
  bool exclusive = false;
  unsigned max_outstanding = 1;
  // Packets sharing a source tag: one normally, four after bounded splitting.
  // Behind a splitter with read granule windows: hint read_taken_packet names the packet with which
  // a read leaves the port pipe (Axi4PacketWindow::read_granule).
  bool read_granule_taken = false;
  unsigned packet_depth = 1;
  // Each source tag gathers its own read beats, for words of different tags that alternate
  // (needs max_outstanding > 1). Otherwise one gather serves one packet at a time.
  bool reassembly = false;
  // Behind the packet splitter: each packet's plan fields come on the hint face beside an up face
  // (<direction>_packet_*), so the piece plan does not wait for the packet's own fields.
  bool write_hints = false, read_hints = false;
  // The source's admission miss window (Axi4PacketizerProfile::source_miss), for the handed key.
  std::optional<AddressWindow> source_miss;
};
Result<NodeHardware> lower_axi4_width_cut(const Axi4WidthCutProfile &profile);

// Source-side per-burst width clamp for per-endpoint attachment: the cutter
// with both faces at the source width and the target width from the window.
struct Axi4WidthClampProfile : ComponentProfileBase {
  unsigned data_bits = 256; // source width: 64, 128, 256, 512 or 1024
  // Routing window and the log2 byte width of its target (2..log2(data/8)).
  std::vector<std::pair<AddressWindow, unsigned>> windows;
  std::vector<std::pair<AddressWindow, unsigned>> piece_len_bits; // Axi4WidthCutProfile
  // Own target windows for the wide FIXED refusal (Axi4WidthCutProfile).
  std::vector<AddressWindow> fixed_reject;
  // Zero the whole read beat when any of its sub-beats failed.
  bool zero_failed_beats = false;
  // Responses are runs (message.hpp point 18): the down face's count field holds the reach.
  bool response_runs = false;
  bool decode_packets = false, decode_flag = false; // Axi4WidthCutProfile
  // Weft exclusive fields (message.hpp contract point 11).
  bool exclusive = false;
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13), passed through
  unsigned data_user_bits_per_byte = 0;
  unsigned response_user_bits = 0;
  unsigned max_outstanding = 1;
  // Behind a splitter with read granule windows: hint read_taken_packet names the packet with which
  // a read leaves the port pipe (Axi4PacketWindow::read_granule).
  bool read_granule_taken = false;
  unsigned packet_depth = 1;
  bool reassembly = false; // Axi4WidthCutProfile
  bool write_hints = false, read_hints = false;
  std::optional<AddressWindow> source_miss;
};

Result<NodeHardware> lower_axi4_width_clamp(const Axi4WidthClampProfile &profile);
// Write ("w.") and read ("r.") state bits of the lowered clamp, for budgets.
std::pair<unsigned, unsigned> axi4_width_clamp_state_bits(const Axi4WidthClampProfile &profile);
} // namespace nocloom::detail
