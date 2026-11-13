#pragma once

#include <cstdint>
#include <nocloom/result.hpp>
#include <vector>

namespace nocloom::detail {
// Target timeout counts a watchdog can take: a power of two, 16..1048576.
inline constexpr bool valid_timeout_cycles(unsigned cycles) {
  return cycles >= 16 && cycles <= 1048576 && (cycles & (cycles - 1)) == 0;
}

// 1..256 initiators and targets; a source code is 2 * index + direction (a 9-bit Weft field); a
// source with several bursts in flight records one of at most 256 routing windows per burst.
inline constexpr unsigned max_endpoints = 256, max_source_bits = 9, max_destinations = 256;

struct MessageParameters {
  unsigned address_bits;
  unsigned data_bits;
  unsigned source_count;
};

enum class MessageFieldKind {
  Address,
  Write,
  WriteData,
  ByteEnable,
  Protection,
  Source,
  ReadData,
  Result
};

struct MessageField {
  MessageFieldKind kind;
  unsigned width;
  bool operator==(const MessageField &) const = default;
};

struct MessageLayout {
  std::vector<MessageField> request;
  std::vector<MessageField> response;
  std::uint64_t request_bits() const;
  std::uint64_t response_bits() const;
};

// Describes payload widths for the Weft message format, a NocLoom private format.
// It is not a public bus standard or a user configuration ABI.
// Endpoint support requires separate validation.
// Handshake signals are not payload fields. A single source has no encoded field.
Result<MessageLayout> describe_messages(MessageParameters parameters);

// ---------------------------------------------------------------------------
// Weft burst packet contract v1 (full AXI4 profile)
//
// The initiator and target adapters, packet transport and burst composition
// share this internal Weft format without a public bus or configuration ABI guarantee.
//
// A *fragment* is one valid/ready transfer in the field-structured shape:
// a control bundle, the byte occupancy `count`, a per-fragment `error`, the
// packet boundary markers `first`/`last`, then the payload fields. A *packet*
// is the fragment sequence from a `first` fragment to the matching `last`
// fragment inclusive; a single-fragment packet carries first == last == 1.
//
// 1. Packet shapes (N = Length + 1 beats, Length is the AxLEN value).
//    - Write burst: the request packet has exactly N fragments; fragment k
//      carries write beat k. The write response packet has exactly one
//      fragment (first == last == 1), produced only after the last request
//      fragment is consumed; its result aggregates the beats (first
//      non-success in beat order wins; beats are ordered).
//    - Read burst: the request packet has exactly one fragment carrying the
//      descriptor (first == last == 1); its payload is zero. The read
//      response packet has exactly N fragments; fragment k carries read
//      beat k.
// 2. Descriptor. The burst descriptor {length, size, burst, cache, id}
//    travels in the control bundle of EVERY request fragment (replication
//    discipline), so every hop learns the packet extent from the head. The
//    lock field exists only in definitions with an exclusive-capable target
//    (BurstProfile::exclusive, see 11).
// 3. Addresses. Every request fragment address is its own beat container
//    base, already advanced and aligned down to the beat size: INCR beat k is
//    align_down(AxADDR, 2^size) + k * 2^size, FIXED repeats the aligned start,
//    WRAP advances inside the wrap region. The single read request fragment
//    carries the aligned burst base. Fragments are self-describing: routing
//    and lane derivation need no beat counting. The raw unaligned start
//    offset survives in the first write beat's byte enables. Real master
//    outlets also receive the optional raw start_address descriptor field.
// 4. count. count == 2^size on every write request fragment and on every read
//    response fragment: this beat's container size in bytes. A read request
//    or write response leaving a width converter has no payload and carries
//    count 4, so one word holds it on every link. Burst packets
//    may carry count 1 or 2 for narrow beats; the floor "multiple of four,
//    at least four" applies to word messages only. count is redundant with
//    size by construction and is carried so hop lane derivation reads it
//    directly.
//    A request reaching a target carries a run of beats (width_adapter.hpp
//    request runs): count is then the run's bytes from its address, and the
//    request-only flag `ends` (after count) says that the run ends at the
//    last byte of the endpoint word (a source's beat: burst_faces.hpp
//    beat_ends; the transport derives it from the run). `wrap_end` (after
//    ends) marks a WRAP write beat that is its container's last
//    (burst_faces.hpp wrap_end_of of the fragment's own fields; zero on
//    reads): the transport joins no beats across it. Every write request
//    producer keeps both consistent with the fields it hands on.
// 5. Payload. A write request fragment carries the beat's WDATA and WSTRB
//    verbatim; bytes outside the beat container are unspecified at the
//    endpoint face and zero-filled at serialization hops, so
//    consumers must qualify data with the byte enables. A read response
//    fragment carries the beat's data in bus-lane (container-aligned)
//    position. The payload is one field of the endpoint bus width; the
//    32-bit lane decomposition is the adapter's concern, not the endpoint's.
// 6. first/last are descriptor-driven: first marks beat 0, last marks beat
//    N-1, both derived from Length. They never come from WLAST; the ingress
//    checks WLAST against the beat position and reports a mismatch as SLVERR
//    on B while keeping the emitted packet contract-exact.
// 7. error is the per-fragment transport error (width_adapter.hpp). Ingress
//    request fragments have error == 0. On response fragments, error == 1
//    maps to SLVERR at the AXI face unless the result already says DECERR.
// 8. result uses the internal codes {0 success, 1 network_miss,
//    2 target_error}; the AXI face maps them exactly like the AXI4-Lite
//    profile: success -> OKAY (EXOKAY with exclusive_okay, see 11),
//    network_miss -> DECERR, anything else or a fragment error -> SLVERR.
// 9. Ordering and capacity: a source defaults to one shared burst slot.
//    A source with capacity N keeps up to N bursts across reads and writes.
//    Every in-flight burst of one AXI ID on a direction goes to one
//    destination, so the packets of one ID travel one
//    route and its responses arrive in issue order. Bursts of different IDs
//    may travel to different targets and their responses may arrive in any
//    order. The request carries `tag` (12), the source's slot of the burst;
//    every target engine returns it unchanged on each response fragment.
//    Fragments of one packet are contiguous on a channel (no interleaving
//    with other packets; merge points hold a grant from first to last).
//    A burst cut into several packets at the source (bounded packets, width
//    cut pieces) may have up to piece_count::kInFlight of them in flight per
//    direction: they share the burst's ID and one destination and come back
//    in order. The single-beat pieces of a narrow-split burst are not part of
//    that limit: the source's shared in-flight pool counts each of them.
//    That pool bounds the transactions of a source but an APB initiator's: reads and
//    writes together keep at most N in flight, one entry per burst, freed at its last answer.
//    Every request packet produces exactly one response packet
//    (one B fragment, or Length + 1 read fragments), including local
//    completions (network miss, target timeout, sub-page remainder).
// 12. Response steering tag. With BurstProfile::tag_bits > 0 (some source
//    keeps more than one burst per direction) both control bundles carry
//    `tag` of that width before id: a source with capacity N numbers its
//    in-flight bursts per direction 0..N-1 and zero-extends the number; a
//    source with capacity one sends zero. A target engine copies the request
//    tag to its response; a definition without tags has no tag field.
// 10. Reset: the common active-low reset cancels in-flight packets together
//    with the network and the targets.
// 11. Exclusive access (AXI A7.2). With BurstProfile::exclusive the request
//    control bundle carries `lock` (1 bit, after cache) and the response
//    carries `exclusive_okay` (1 bit, after result). lock is set only on a
//    burst that passed the ingress exclusive checks and goes to a target with
//    exclusive support; elsewhere AxLOCK is dropped at the ingress and the
//    burst is a normal access. exclusive_okay is 1 only on a response
//    fragment of a lock burst that succeeded exclusively: every read beat
//    the exclusive read returned without error, and the B of an exclusive
//    write that updated the target. result keeps its codes {0, 1, 2}: an
//    exclusive write that fails its reservation answers result 0 with
//    exclusive_okay 0 (AXI OKAY). Wide beat gathering (width clamp or a
//    fixed-width cut) ANDs exclusive_okay, so any failed part fails the whole beat;
//    an upsize merge copies a target word's bit to each source beat it holds.
//    Bounded packets never cut a lock burst, so its single B passes
//    unmerged. The ingress maps result 0 with
//    exclusive_okay 1 to EXOKAY; a transport error or a nonzero result wins.
// 13. User flags. With BurstProfile::user_bits > 0 the request control
//    bundle carries `user` (after lock): the network user flags its source
//    drove, the same on every packet and fragment of a burst. Every hop
//    passes it unchanged; a target outlet maps it onto its AxUSER or PAUSER port. With
//    response_user_bits > 0 the response bundle carries `user` (after
//    exclusive_okay): every fragment of a response packet holds the target's
//    user of the packet's first R beat, or of its B (zero from a target
//    without the port and on local answers). APB5 uses PBUSER: the first
//    completed physical transfer of each read or write packet. Timeout write responses carry zero.
// 14. Route path. With BurstProfile::path_bits > 0 both control bundles carry
//    `path` after source: on a request the route class of its captured AxID
//    from the total ID interval partition, stamped where the
//    source enters the network; on a response the return path of its route
//    (the index of the route's switches among the distinct switch sequences
//    of the routes back to its initiator), stamped where the target's
//    responses enter the network from their source, AxID and target. A switch
//    input whose routes of one destination leave by different outputs selects
//    by it. Endpoint engines neither drive nor see the field.
// 15. Scheduling level. A request carries `level` after path where some
//    source maps its AxQOS (InitiatorQos::axqos_map): the map's level of its
//    burst, captured with the burst (zero from a source without a map), the
//    same on every packet and fragment. In the transport every packet of a
//    network with scheduling levels carries it (BurstProfile::level_bits on
//    requests, response_level_bits on responses): a request the level its
//    source gives its head (a fixed level, the map's or the regulator's), a
//    raise packet its raise level, a response the level of its target
//    context. Pipes and targets read it at packet heads; switches arbitrate
//    by the pressure sideband (Channel::level_hint in the composition), not by it.
//    A network with a regulator marks raise packets with the transport-only
//    request bit `raise` after target.
// 16. Transport layout. Endpoint faces carry the global address. Between a
//    source and a target the address keeps only its low address_bits, enough
//    for the aligned block of every target's windows (with the page above a
//    default target's); the target restores the block's high bits. A request
//    carries `target`, the destination index (a target, or a flow of a target
//    of several) the source decoded from the global address, and switches
//    route requests by it (responses by source). The
//    raw start travels as its 4 KiB page offset (start_offset): a burst never
//    leaves its page, so every fragment address holds the start's page and
//    the target joins the two back into start_address.
//    Field order of the control bundles, with the response steering tag of
//    point 12 before id in both directions:
//      request:  address write protection length size burst cache [lock]
//                [user] [record] [security] [start_address | start_offset] [tag] [id] [source]
//                [path] [level] [target] [raise]
//      response: address result [exclusive_okay] [user] [tag] [id] [source] [path]
//                [level]
//    followed by extent ends wrap_end (requests) or reach [wrap_end] (responses, point 18), then
//    error first last [cut] data [be] (be on requests, cut on responses of point 17).
// 17. Response segments. With BurstProfile::segments the response transport
//    carries `cut` after last: an AXI4 target whose read data interleaves IDs
//    (Axi4OutletProfile::interleave) ends a response segment at a fragment
//    that is not its packet's last, where the next response word of the
//    target belongs to another packet or the target's R paused; behind a
//    target merge only at the last source beat of a target word. Every
//    response arbiter releases its grant at last or cut; a later segment of
//    the packet continues it (first stays the packet's first fragment), and
//    only last ends the packet for its source. Endpoint faces never carry cut.
// 18. Response runs. In the response transport a fragment is a run of target words of one packet:
//    from its address to `reach`, its last byte's offset in the fragment's word, in place of count
//    (address | count - 1 at the target; at a source the deframed count field holds the reach).
//    Where a response word widens, one assembler gathers the words of a packet until its word's
//    top, its last, a segment's end (point 17) or `wrap_end`. `wrap_end` is the marker carried
//    after reach with BurstProfile::response_runs: a target's beat that gathers nothing (a FIXED
//    beat, one narrower than the target's word) or its WRAP container's top. The words of a run
//    share one result and error. A word that answers otherwise waits, and the words gathered
//    before it leave first as a run that ends below it, marked with `wrap_end`. So no failure
//    reaches a neighbour.
//    A serializer can split a run and an assembler can rejoin its pieces. The source width clamp
//    gathers only the target contributions to one source beat: network_miss before target_error
//    before success. A failed source beat has zero data. Other source beats keep their own
//    result and data.
// 19. Local miss descriptors. A known local miss with no target access may travel as one tagged
//    response fragment (first and last, count 4), however many beats it answers. Its data holds
//    the length in the low bits of every four-byte group (decode_packet.hpp). A width clamp or
//    target merge restates the length in the beats it answers, and the source expands it to
//    zero-data error beats. Native target responses carry no descriptor tag, including DECERR:
//    result 3 marks the descriptor, or decode_flag reserves error=1 with network_miss for it.
//    In decode_flag profiles native failures use result alone, with error=0.
//
// Burst capability of one endpoint. id_bits is the AxID field width (0..16;
// zero omits the id field and the ID ports entirely). len_bits is the AxLEN
// field width: 4..8, where 8 encodes packets of 1..256 beats.
struct BurstProfile {
  unsigned id_bits = 0;
  unsigned len_bits = 8;
  // Separate raw AxADDR from aligned packet occupancy; only true master outlets use it.
  bool start_address = false;
  unsigned address_bits = 32; // endpoint layout: the global width, 32..48
  // Transport fragment occupancy width (fragment_fields), one size for every
  // ladder level of a composition: six bits carry words of up to 32 bytes,
  // seven and eight bits the 64- and 128-byte endpoint words of 512- and
  // 1024-bit attachments. Endpoint layouts derive their count from the data
  // width (burst_count_bits) and ignore this field.
  unsigned occupancy_bits = 6;
  // Exclusive access fields (contract point 11): request lock, response
  // exclusive_okay. False keeps the layout of definitions without them.
  bool exclusive = false;
  // Response steering tag width (contract point 12); zero omits the field.
  unsigned tag_bits = 0;
  // Network user flags (point 13); zero omits the field.
  unsigned user_bits = 0;
  // Response user field (point 13); zero omits it.
  unsigned response_user_bits = 0;
  // Route path field (point 14); zero omits it.
  unsigned path_bits = 0;
  // Scheduling level fields (point 15) of requests and of responses; zero omits each. raise adds
  // the request raise packet bit.
  unsigned level_bits = 0;
  unsigned response_level_bits = 0;
  bool raise = false;
  // Transport layout (point 16): the raw start's page offset in place of start_address (12 bits),
  // and the request route field; zero omits each.
  unsigned start_offset_bits = 0;
  unsigned target_bits = 0;
  // Response segments (point 17): the transport response marker `cut`.
  bool segments = false;
  // Response runs (point 18): the transport response marker `wrap_end`.
  bool response_runs = false;
  // A request's first-error record fields fixed at its source, for a network with an error logger;
  // zero omits the field.
  unsigned record_bits = 0;
  // A request's security flags, for a network with an error logger; zero omits the field.
  unsigned security_bits = 0;
  // Point 19: a local miss may travel as a tagged word with its length as data.
  bool decode_packets = false;
  // Reserves the error flag for that descriptor instead of result code 3 (decode_packet.hpp).
  bool decode_flag = false;
  // User bits per payload byte. Zero omits the data_user field.
  unsigned data_user_bits_per_byte = 0;
  bool operator==(const BurstProfile &) const = default;
};

// Burst packet fragment fields in travel order, handshake aside: control
// bundle, count, error, first, last, then payload data (and byte enables on
// requests). The caller validates the profile (the endpoint lowering rejects
// out-of-domain parameters before deriving fields).
// Byte occupancy includes the full beat, retaining six bits through 256 data bits.
unsigned burst_count_bits(unsigned data_bits);

std::vector<std::pair<std::string, unsigned>>
burst_request_fields(unsigned data_bits, BurstProfile profile, unsigned source_bits);
std::vector<std::pair<std::string, unsigned>>
burst_response_fields(unsigned data_bits, BurstProfile profile, unsigned source_bits);

} // namespace nocloom::detail
