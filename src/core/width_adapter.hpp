#pragma once
#include "component.hpp"
#include "message.hpp"
#include <optional>

namespace nocloom::detail {
// Serialization ladder, parameterized 2:1 hop family H(2w <-> w).
//
// An adapter instance is derived from a transport word width difference at one cut point; it is
// never a permanent layer. Equal word widths derive zero instances and the cut stays a plain
// pass-through, so no module is planned or lowered. Only 2:1 hops exist: 32<->16, 16<->8 and 8<->4
// share this parameterization; wider jumps are rejected so intermediate levels are planned instead
// of skipped. A burst profile whose occupancy is widened to 7 or 8 bits
// (BurstProfile::occupancy_bits) extends the family with 64<->32 and 128<->64, used only at 512-
// and 1024-bit endpoint attachments. Hops chain: the wide fragment of one hop is the narrow
// fragment of the next, so every ladder-global field must mean the same at every level.
//
// Fragment model (NocLoom private field-structured expression, not a packed bit layout): one
// fragment carries a control bundle, a ladder-global byte occupancy, up to `lanes` data lanes, a
// per-fragment error indication and first/last packet boundary markers, moved by a valid/ready
// handshake. A lane is one 32-bit data word plus its 4-bit byte enable on requests; response lanes
// are data only. Both directions carry the full address: the request address is the endpoint's, the
// response address is its transport-level shadow, and each hop re-derives its used lane range from
// the low address bits instead of consuming them, so the offset is never rewritten.
//
// The occupancy `count` is the message payload size in bytes (a multiple of four, at least four, at
// most the widest word); the used lanes of a word are the byte range [address % word_bytes, address
// % word_bytes + count). The range never wraps a word boundary. Unused lanes are not transported:
// sub-fragments zero the slots outside the used range and the gathering side emits unused wide
// lanes as zero.
//
// Event semantics per direction:
// - Serializer (wide to narrow): the wide fragment is held until its last
//   sub-fragment is accepted (wide_ready = is_last & narrow_ready). Control
//   and occupancy are replicated per sub-fragment, the error indication fans
//   out to every sub-fragment, and first/last are rebuilt so only the initial
//   sub-fragment carries first and only the final one carries last.
// - Deserializer (narrow to wide): the completing narrow fragment is held
//   until the gathered wide fragment is accepted
//   (narrow_ready = ~completes | wide_ready). Control is captured from the
//   first fragment, per-fragment errors are OR-accumulated across the gather,
//   and non-completing fragments are always accepted into the lane registers.
//
// This is an internal profile: no public schema, configuration key or CLI path
// is added, and endpoint transaction semantics are out of scope here.
//
// Response runs (burst set, responses only, message.hpp point 18): a response fragment is a run of
// target words of one packet, from its address to `reach` in place of count. The narrow-to-wide
// side is one assembler of any ratio on each widening link: wide_valid = valid & ends, narrow_ready
// = ~flush & (~ends | wide_ready), ends = last | cut | wrap_end | the word's position is the top.
// Each word below the top is stored in its place. flush = stored words & the arriving word's
// result or error differs from theirs: the stored words leave as a run that ends below it, marked
// with wrap_end, and the arriving word waits. A serializer splits a run as it does a request run.
//
// Burst carriage (burst composition): when `burst` is set, the control bundle extends with the Weft
// burst contract's descriptor fields (message.hpp), replicated per fragment like the rest of the
// bundle, and the occupancy check widens to byte granularity so burst beats with count 1 or 2
// (narrow AxSIZE) select their lanes correctly. The word-message floor (count a multiple of four)
// still applies when `burst` is unset, and the widened cone degenerates to the word rule for
// word-aligned occupancies.
//
// Request runs (burst set, requests only): a request fragment is a run of whole beats of its
// burst or a part of one beat, from its address to its last byte `reach` (the field in place of
// count): the byte offset in the fragment's own word, as wide as that offset, so a hop word
// carries no more bits than it can name. `ends` repeats whether reach is the word's last byte
// (for the endpoints; the hops read reach), and `wrap_end` whether the run's last beat is its
// WRAP container's last (message.hpp point 4: the source marks it). The serializer gives the high
// part of a split run its own start at the half boundary and the low bits of the reach; the low
// part ends at the narrow word's last byte and is no container's end. The deserializer joins two
// runs into one wide run: a narrow run that lies in the low half, ends at the half boundary and
// is not the packet's last waits for the next run (it starts at that boundary: the rest of a
// split beat, or the next beats of an INCR, or of a WRAP whose container does not end there), as
// a transport word gathers the beats of its packet up to its word boundary or the packet tail.
// Whole beats join only while `merge` allows (with merge_gate, the nl_narrow_merge input of the
// narrow side; a FIXED burst never). The joined run ends where the second ends: its reach is the
// second's below the half bit of its address. Responses keep the replicated rule above.
struct WidthAdapterParameters {
  unsigned wide_word_bytes = 32;
  unsigned narrow_word_bytes = 16;
  bool response = false;
  unsigned source_bits = 0;
  std::optional<BurstProfile> burst;
  // Deserializer of request runs only: whole beats join while the nl_narrow_merge input is high
  // (absent: always).
  bool merge_gate = false;
  bool operator==(const WidthAdapterParameters &) const = default;
};
struct WidthAdapterPlan {
  WidthAdapterParameters parameters;
  unsigned lanes_wide = 0, lanes_narrow = 0;
  // Ladder-global byte occupancy width; one size fits every level.
  unsigned count_bits = 0;
  // Equal word widths: zero adapter instances, pure pass-through, no module.
  bool pass_through = false;
  bool operator==(const WidthAdapterPlan &) const = default;
};
// Derives the hop shape; unsupported widths and ratios are diagnostics.
Result<WidthAdapterPlan> plan_width_adapter(const WidthAdapterParameters &parameters);
// Lowers the wide-to-narrow fragment serializer. Equal widths are a diagnostic:
// there is no adapter to lower on a pass-through cut.
Result<NodeHardware> lower_width_serializer(const WidthAdapterParameters &parameters);
// Lowers the narrow-to-wide fragment deserializer with the same derivation.
Result<NodeHardware> lower_width_deserializer(const WidthAdapterParameters &parameters);
// Canonical fragment field order (name, width) for composed channels carrying one hop level,
// handshake aside: control bundle, occupancy, error, first, last, then per-lane data (and byte
// enables on requests). With `burst` set the control bundle carries the burst descriptor (and ID)
// fields in the Weft burst contract's order, so an endpoint word level matches
// burst_request_fields/burst_response_fields field for field.
std::vector<std::pair<std::string, unsigned>>
fragment_fields(bool response, unsigned word_bytes, unsigned source_bits,
                std::optional<BurstProfile> burst = std::nullopt);
} // namespace nocloom::detail
