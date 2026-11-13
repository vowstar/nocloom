#pragma once
#include "component.hpp"
#include "id_class.hpp"
#include "message.hpp"
#include "nocloom/definition.hpp"

namespace nocloom::detail {
// Full AXI4 initiator (ingress) endpoint: accepts AXI4 bursts on an AW/W/B/AR/R slave face and
// packetizes them into Weft burst packets per the contract in message.hpp. The schema 10 backend
// composes this internal endpoint with packet routes and real AXI4 master outlets.
//
// Internal slots are per direction. The composition shares source capacity across reads and writes.
// With max_outstanding N > 1 (full AXI4 only) each direction keeps:
// - a slot table of N bursts accepted at the AXI face and not yet answered
//   (AWREADY/ARREADY low while it is full); the slot number is the Weft tag
//   of the burst's request, and every response returns it, so a response
//   finds its burst (ID, length, WLAST poison) whatever its arrival order;
// - a binding table {AXI ID, destination, count} with an entry for every ID
//   the face can have in flight (bound_ids): every burst of one ID in flight
//   goes to one destination, the target whose routing window holds its start
//   address or the local completion of an admission failure. A burst whose
//   ID is bound to another destination waits after its address handshake
//   until the last response of that ID has entered the engine (a network
//   response packet loaded, or a local completion taking the face); while it
//   waits it shows on the hint face (write_waiting, read_waiting), so the
//   request merger can hold the other direction behind it. The bursts of one
//   ID then travel one route and their responses arrive in issue order (AXI
//   A5.3, A6); different IDs may answer in any order. With crossing the
//   entry follows the destination of the last beat of its newest burst. The
//   source's ID table proper, {direction, ID} keys shared by both
//   directions, lives at the request merger (Initiator::id_table_entries).
// - one B register: a response packet or a local completion enters it while
//   it is empty, and BREADY empties it.
// R beats pass in arrival order, so bursts of different IDs may interleave beat by beat; a local
// completion takes the face when no network response waits. W beats follow their AW without
// interleaving: a W beat waits for its AW, so a master leading with W sees backpressure (legal).
//
// Admission checks run when the descriptor is captured; a violating burst completes locally with
// SLVERR and never emits a request fragment (an AXI4-Lite ingress instead clears its address low
// bits first):
// - an exclusive burst (AxLOCK) whose start address lies in an exclusive
//   window and that breaks the exclusive envelope there: a power-of-two beat
//   count whose total bytes stay within the window's byte limit (128, or 16
//   target words at an AHB target, never cut across its packet limit), the
//   address aligned to its total bytes, not FIXED, and beats of at least the
//   window's minimum size (4 bytes for a target without write strobes),
// - AxSIZE above the bus width, AxBURST == 3 (reserved),
// - INCR span crossing a 4 KiB boundary,
// - WRAP with length not in {2, 4, 8, 16} or a start address not aligned to
//   the beat size,
// - FIXED with length above 16 beats (only expressible with len_bits == 8;
//   AXI4 extends the burst length beyond 16 for INCR only).
// WLAST must mark exactly beat N-1; a mismatch still emits a contract-exact packet (boundaries are
// descriptor-driven) and completes B with SLVERR.
//
// Exclusive access: AxLOCK outside every exclusive window is ignored and the burst is a normal
// access, answered OKAY at best (AXI A7.2.3, A7.2.5). An admitted exclusive burst inside an
// exclusive window carries lock = 1, and a response with exclusive_okay answers EXOKAY.
struct ExclusiveWindow {
  AddressWindow window;     // the target's routing window
  unsigned max_bytes = 128; // largest exclusive access in bytes (power of two)
  unsigned min_size = 0;    // smallest AxSIZE of an exclusive beat
};
// A window of the source's packet splitter (axi4_packetizer.hpp), listed in its decode order. A
// locked INCR (AxLOCK set; a locked WRAP is sent as INCR) that the splitter would cut fails whole
// with SLVERR and reaches no target: the first window holding its start decides, as at the
// splitter, and one not checked (a decode miss, or a target that checks exclusive access itself)
// never fails it.
struct LockWindow {
  AddressWindow window;
  std::uint64_t max_bytes = 0, boundary_bytes = 0, align_bytes = 0;
  bool checked = true;
};
// A mode window of the source decode: a global window of a mode-dependent mapping, the flags its
// condition reads (care) with their values, and its plane of the transport address space.
struct ModeWindow {
  AddressWindow window;
  std::uint32_t care = 0, value = 0;
  unsigned plane = 0;
  bool operator==(const ModeWindow &) const = default;
};
struct Axi4InitiatorProfile {
  std::string name;
  unsigned data_bits = 32;             // 32, 64, 128, 256, 512 or 1024 (Lite: 32 or 64)
  unsigned address_bits = 32;          // APB 1..32, Lite 2..48; full AXI explicit transport 12..48
  unsigned id_bits = 0;                // 0..16; zero omits the ID ports and the id field
  unsigned len_bits = 8;               // 4..8 (AxLEN field width)
  bool read = true;                    // AR/R face and read packetization
  bool write = true;                   // AW/W/B face and write packetization
  unsigned source_bits = 0;            // 0 standalone; >0 reserves a source field
  bool start_address = false;          // emit the original AxADDR alongside aligned occupancy
  unsigned transport_address_bits = 0; // zero derives max(32, address_bits)
  // Single-beat AXI4-Lite ports with independently held AW/W. AxADDR is cleared to the bus width
  // before every check; WSTRB passes as given.
  bool lite = false;
  // Apb5, Apb4 or Apb3: an APB completer face replaces the Lite ports of a 32-bit Lite engine (lite
  // set, at most 32 address bits). PSEL offers AW and W, or AR, which the idle engine takes in
  // SETUP; BREADY and RREADY are PSEL & PENABLE, so PREADY is the registered B or R valid and the
  // engine is idle again when the transfer completes. PSLVERR tells whether the last response was
  // other than OKAY, and PRDATA is zero unless the last is a read OKAY. APB3 has no PSTRB or PPROT:
  // every write strobes all bytes and the protection is unprivileged, Non-secure, data (PPROT 2).
  std::optional<Protocol> apb = std::nullopt;
  unsigned apb_data_bits = 32; // Physical APB face: 8, 16 or 32; engine stays 32.
  // An AHB-Lite subordinate face (IHI0033A) replaces the ports of a full AXI4 engine of the same
  // width with one burst per direction. Each AHB burst becomes AXI4 bursts: SINGLE one beat of
  // its HSIZE, INCR4/8/16 and WRAP4/8/16 one burst of their beats, an undefined-length INCR
  // pieces of four words from each piece's start (to the 1 KiB boundary in its last four-word
  // block), and a non-bufferable undefined-length INCR write one burst per beat. A piece the
  // manager ends early is padded with unstrobed W beats or its spare R beats are dropped.
  // Bufferable writes (HPROT[2]) complete on W acceptance and ignore their B; any other write
  // ends with its burst's B. A failed beat answers the two-cycle ERROR. HMASTLOCK, a beat
  // wider than the bus and a narrow beat outside SINGLE answer ERROR without access. Requests
  // issue only after every earlier write response, so transfers keep program order.
  bool ahb = false;
  bool ahb5 = false;
  unsigned hmaster_bits = 0, ahb5_metadata_bits = 0, hprot_bits = 4;
  bool hnonsec = false;
  // AMBA 3 AXI ports (IHI0022B) on a full AXI4 engine: AWLOCK and ARLOCK are two bits, and W
  // carries WID. AxLOCK 0b01 is exclusive; 0b10 (locked) and 0b11 (reserved) take the reserved
  // AxBURST encoding, so the burst completes locally with SLVERR. W beats follow their AW in order
  // (no write interleaving): a beat whose WID differs from its burst's AWID poisons the burst like
  // a WLAST mismatch, so B answers SLVERR.
  bool axi3 = false;
  // The AW and AR ports sit behind port pipes that take the source's handshakes (a composed
  // network's source). A full AXI4 or AXI3 engine then leaves AW and AR in the pipe until the
  // burst goes: AW with its last W beat, AR once the network takes its request (the hint input
  // read_taken: its final packet enters the source's request merger). One burst starts at a time
  // per direction (with several, while a slot is free); with one burst the next starts in the
  // cycle the previous burst's B or last R beat leaves.
  bool port_pipes = false;
  // Optional source admission windows. A miss completes locally with DECERR, or, with
  // miss_window, leaves as an error request at that window (the default target's): its address
  // moves into the window, a write sends one fragment after its last W beat, and the target
  // answers DECERR without access.
  std::optional<std::vector<AddressWindow>> admitted_windows = std::nullopt;
  std::optional<AddressWindow> miss_window = std::nullopt;
  // An error logger records a miss's address: it keeps these bits of its own, the rest of base.
  struct MissKept {
    std::uint64_t base = 0, bits = 0;
    bool operator==(const MissKept &) const = default;
  };
  std::optional<MissKept> miss_kept = std::nullopt;
  // Destinations (their index) inside a window it reaches, of targets it does not reach: its decode
  // does not see them.
  std::vector<unsigned> hidden_destinations = {};
  // The record field of its requests (BurstProfile::record_bits): a lock bit of a decode miss above
  // the burst's bytes less one, less the start's offset in its first beat.
  unsigned record_bits = 0;
  // BurstProfile::security_bits, and the driver of every security flag in name order: a constant,
  // an AxPROT flag or an AxUSER bit.
  unsigned security_bits = 0;
  std::vector<AttributeBit> security_flags{};
  // Total source ID partition and, parallel to
  // admitted_windows, the flows each admits (bit k for flow k): a burst of another flow misses
  // there. Empty admits every flow in every window.
  std::vector<std::uint64_t> admitted_flows = {};
  std::vector<IdClassInterval> flows = {};
  // Report every failure as SLVERR: decode and admission misses and any DECERR returned through the
  // network.
  bool uniform_slverr = false;
  // Bursts per direction in flight, 1..64. Above one the profile must be full AXI4 and must give
  // the destination decode below.
  unsigned max_outstanding = 1;
  // With max_outstanding above one: the AXI IDs per direction the engine binds to destinations,
  // every one the face can have in flight (fewer when the face's own ID port is narrower than
  // id_bits); zero derives min(max_outstanding, 2^id_bits).
  unsigned bound_ids = 0;
  // Weft tag field width (message.hpp point 12); at least bit_width(max_outstanding - 1) above one,
  // where zero derives that width; with one burst per direction zero omits the field, and a nonzero
  // width sends zero and ignores returned tags.
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13)
  unsigned response_user_bits = 0;
  // Weft request level field width (message.hpp point 15), and the full AXI face's AxQOS map
  // (InitiatorQos::axqos_map): a nonempty map adds AWQOS and ARQOS ports of log2(size) bits and
  // drives the field with the entry of the burst's AxQOS; an empty one drives level zero.
  unsigned level_bits = 0;
  std::vector<unsigned> axqos_map{};
  // AXI faces use AxUSER and APB5 uses PAUSER, with one driver per network flag.
  unsigned user_port_bits = 0, response_user_port_bits = 0;
  std::vector<AttributeBit> user_flags{};
  // The request routing windows in target order: the destination of an admitted burst is the target
  // whose window holds its start address.
  std::vector<AddressWindow> destinations{};
  // Destination index of a network miss: the miss_window's target, or without admission windows
  // the target a miss reaches with its own address.
  std::optional<unsigned> miss_destination = std::nullopt;
  // A burst may cross destination windows (an admitted page shared by targets): an ID is bound to
  // the destination of its last burst's last beat, and a burst issues under it when it starts
  // there.
  bool crossing = false;
  // The source's request merger checks the ID binding of network bursts behind a pipe: the engine
  // keeps only the order between network bursts and local completions of one ID.
  bool late_id_binding = false;
  // Weft exclusive fields (message.hpp contract point 11) and the windows of the targets with
  // exclusive support this source reaches. Empty windows with exclusive set keep the fields with
  // lock always zero.
  bool exclusive = false;
  std::vector<ExclusiveWindow> exclusive_windows{};
  std::vector<LockWindow> lock_windows{};
  // Source AxLOCK beside the current request, before exclusive admission masks the Weft lock. Only
  // the neighbouring packet splitter consumes it.
  bool source_lock = false;
  // The burst's start, the write beat's address and AxLEN before a miss moves them, beside the
  // request (hint face: write_start, write_address, write_length, read_start) for the splitter,
  // which cuts a missed burst: a missed write then sends every beat.
  bool address_hints = false;
  // Mode-dependent decode: the address selectors arrive on the hint face (address_selectors,
  // address_selector_bits wide). The burst in service takes the plane of the first mode window
  // holding its start whose selectors match (zero outside them) as transport address bits
  // plane_shift and up; every window, admission and destination above is a transport window. The
  // plane follows the selectors until the network takes the burst's request (a write: its first
  // fragment, or its first beat taken locally); a write then holds it until it leaves.
  unsigned address_selector_bits = 0, plane_shift = 0, plane_bits = 0;
  std::vector<ModeWindow> mode_windows{};
  bool operator==(const Axi4InitiatorProfile &) const = default;
};

// Lowers the endpoint. Out-of-domain parameters are diagnostics, never silently clamped. The
// write-only shape (read == false) is rejected: only read-write and read-only sockets are provided.
Result<NodeHardware> lower_axi4_initiator(const Axi4InitiatorProfile &profile);

} // namespace nocloom::detail
