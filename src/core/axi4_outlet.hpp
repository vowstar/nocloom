#pragma once
#include "id_class.hpp"

#include "component.hpp"
#include "exclusive.hpp"
#include <nocloom/definition.hpp>
#include <optional>

namespace nocloom::detail {

// Internal burst-preserving AXI4 master. Inputs are admitted, well-formed burst packets with an
// originally beat-aligned start address, or the explicit start_address extension for unaligned
// AxADDR. No split, coalescing or CDC is provided. One burst (read or write) is outstanding unless
// max_outstanding allows more (see below); the pool is shared by both directions. W beats pass
// through one forward entry (a register that takes the next beat while it is empty or its beat
// leaves, so the network W ready follows WREADY) and R beats through a two-entry stage, one beat
// per cycle without stalls; behind a target merge (merged) R takes one entry instead, and with
// interleave R and B pass the joined stage described there. A write head
// word's AW and first beat enter apart: the AW takes the main entry (one AW stage) and the beat the
// W entry, each when it has room, and the word leaves when both are in. With several bursts a
// read waits for its AR in one AR stage, so AWREADY and ARREADY reach the network write and read
// readies in the same cycle (as the W entry's WREADY does). No other target-side ready or valid
// reaches a network-side output in the same cycle (a write-only outlet's B and an interleaving
// outlet's BVALID and RVALID aside, see interleave), nor the reverse. The target must complete all
// descriptor-specified beats, including after errors. A common reset cancels the outlet, transport
// and target together.
// The constant part of a target's route word (Axi4OutletProfile::id_route). AxID lies
// direction_bits up, and a source with direction takes its write direction in bit 0.
struct TargetIdRoute {
  struct Source {
    // Per source route class: its number in the route word and the total ID partition.
    std::vector<std::uint64_t> flows{};
    std::vector<IdClassInterval> ranges{};
    bool direction = false;
  };
  std::vector<Source> sources{};             // parallel to id_sources
  std::vector<std::uint64_t> target_paths{}; // per target flow: its number in place
  unsigned direction_bits = 0;
};
struct Axi4OutletProfile {
  std::string name;
  unsigned data_bits = 32;
  unsigned id_bits = 0;
  unsigned len_bits = 8;
  unsigned source_bits = 0;
  bool start_address = false;
  // Global input, the burst at the local address of the mapping holding its start (whole 4 KiB
  // pages or power-of-two sub-pages of at least one word, in base order); none passes every
  // address. Each packet decodes once: a packet never crosses a mapping edge (the source cuts
  // it there), and one starting outside every mapping answers DECERR on every beat.
  std::vector<AddressMapping> mappings;
  unsigned address_bits = 32;
  unsigned transport_address_bits = 0; // zero derives max(32, address_bits)
  // Arrival block of the outlet's global addresses (AddressBlock).
  AddressBlock arrival{};
  // Absorb every write locally: all W beats drain, then one SLVERR B, and the
  // AW/W/B faces never assert.
  bool read_only = false;
  // Answer every read locally: SLVERR with zero data on every beat, and the AR/R faces never
  // assert. A B taken while no earlier B waits reaches the network in the same cycle.
  bool write_only = false;
  // Compact a known local miss into a tagged length word. Native target RRESP is mapped
  // independently on every beat. No target error changes the following beats.
  bool error_codes = false;
  bool decode_flag = false; // BurstProfile::decode_flag
  // Mapped AxCACHE/AxPROT; absent carries the source attributes through.
  std::optional<TargetAttributes> attributes = std::nullopt;
  // Zero builds no timer. Otherwise cycles of the watchdog, a power of two from 16 to 1048576
  // (Logic::watchdog) fail the target until the common reset: later beats and bursts are absorbed
  // after the window decode and answer SLVERR with timeout_completion, else nothing. An asserted
  // AWVALID, WVALID or ARVALID stays with its payload (a frozen copy) until accepted, no further W
  // beat is sent, BREADY and RREADY stay high, and nl_status_timed_out goes high.
  unsigned timeout_cycles = 0;
  bool timeout_completion = false;
  // Bursts in flight at the target, reads and writes combined in one pool, 1..16. A burst holds an
  // entry from its head until its last response word leaves; after a timeout every one of them
  // completes locally in its usual order. Reads of
  // any target IDs overlap: an R belongs to the oldest read of its RID not yet received; an R
  // naming no read answers SLVERR for its beat. Unless interleave is set the beats of one read
  // arrive together, and an R with another RID meanwhile answers SLVERR as well. Writes of any
  // target IDs overlap. BREADY accepts only a B whose ID names the oldest write of that target ID
  // still in flight; any other B (a protocol error) is left waiting. Window misses and error
  // requests complete locally, a write before any later write of its target ID is answered, a read
  // once every earlier read is received. With several bursts an entry returns to the pool in the
  // cycle after its last response word left.
  unsigned max_outstanding = 1;
  // The global indices of the sources reaching the target, with their route in id_route. Besides
  // its flow number a target ID holds the sum of the ID-wide slices of the route word, so requests
  // of different sources may share one target ID and wait for each other as the AXI ordering rules
  // ask of one ID. The port carries that value rearranged (id_to_port in the lowering).
  std::vector<unsigned> id_sources{};
  TargetIdRoute id_route{};
  // Weft response steering tag width (message.hpp point 12); every response
  // returns its request's tag. Zero omits the field.
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13)
  unsigned data_user_bits_per_byte = 0;
  unsigned response_user_bits = 0;
  // AxUSER bit i: a constant or AttributeSource::User naming a flag by index; empty has no port.
  std::vector<AttributeBit> user{};
  unsigned response_user_port_bits = 0; // BUSER and RUSER inputs; none at zero
  // Weft exclusive fields (message.hpp contract point 11). Without exclusive
  // support AxLOCK stays 0 and exclusive_okay is never set. exclusive_target:
  // - network (one burst in flight, a read-write target): the outlet's
  //   monitor (exclusive_monitor.hpp) judges every exclusive access. An
  //   exclusive read goes out as a normal read and answers exclusive_okay on
  //   each beat without error; an exclusive write without a matching
  //   reservation goes out with zero WDATA and WSTRB, so it changes nothing,
  //   and answers result 0 without exclusive_okay (AXI OKAY) whatever its B.
  // - target (a read-write target): AxLOCK carries lock and an exclusive
  //   access takes the value of the owner flags (ExclusiveTarget::id_flags) below its
  //   flow number instead of the sum, so the target's own monitor tells the owners
  //   apart. An
  //   EXOKAY to a lock burst answers result 0 with exclusive_okay, an OKAY
  //   result 0 without it; EXOKAY to a normal burst stays a target error.
  bool exclusive = false;
  ExclusiveTarget exclusive_target{};
  // Target AxID and AxLEN port widths (absent: internal). AxLEN is truncated or zero-extended; the
  // ID port takes the target ID of that width.
  std::optional<unsigned> port_id_bits = std::nullopt, port_len_bits = std::nullopt;
  // A target merge (axi4_upsize.hpp) takes the R beats: without interleave each waits in one entry,
  // registered as in the two-entry stage, and RREADY is that entry's room: empty, or leaving to the
  // merge in this cycle (the merge takes a word while its word register is empty; a beat it passes
  // through unchanged leaves with the network's ready). With interleave the
  // stage keeps its main entry until the merge takes the word's last source beat, and the read's
  // pool entry returns with the merge's last beat (the hint input read_tail). Unless merged, the
  // request transport offers one packet at a time (the network's steer by direction), so write and
  // read heads never arrive together; merged, a read head enters only while a write head word
  // waiting here leaves an entry. A request word offered stays offered with its payload until taken
  // (the network's arbiters hold an offered head), since with several bursts its AW may enter
  // before the word leaves.
  bool merged = false;
  // Merged with one burst and nothing stored between the merge and the outlet: the request faces
  // are steered from one transport, so a read and a write head are never offered together, and
  // pool.read_ok reads the pool alone.
  bool one_head = false;
  // Heads are admitted ahead of the outlet (a forward stage after admission sits before it, the
  // target's dispatch pipe): the pool counts the hint inputs write_admit and read_admit (a head
  // entering that stage) and the outlet takes heads without checking the pool; pool.write_ok and
  // pool.read_ok tell the stage whether a head may enter.
  bool admitted = false;
  // With several bursts, the network's response merger follows the outlet: the hint inputs
  // write_picked and read_picked are high while it offers the outlet's B or R (taken or not). A B
  // or R in the main entry that it does not offer holds BREADY or RREADY low, as a one-entry pipe
  // per direction ahead of one response register for both would (BREADY and RREADY read no
  // network ready). A B whose ID names no write whose turn has come (a protocol error, or a write
  // behind a local completion of its target ID) parks in one register, BREADY low, until it does.
  bool merger_hints = false;
  // Interleaved read data (an outlet that serves multi-beat reads, with several flows, a nonzero ID
  // width or writes). With several bursts, R
  // beats of different target IDs may arrive interleaved, each filling the oldest read of its RID
  // not yet received; B and R each pass a one-entry backward pipe to a merge and one response
  // register shared by both directions. With one burst, the ordinary two-entry R stage and B
  // register feed a direction join: no second read can interleave. In either case a response offers
  // its word once the next word is there, the word is its packet's last, or one cycle after it
  // found no next word, so the target valids reach the response face in the same cycle. A segment
  // ends (hint output cut, message.hpp point 17) at a word that is not its packet's last where the
  // next word belongs to another packet, or two cycles pass without one. Both response faces share
  // the network ready, and at most one is valid. The network keeps no response merger of its own
  // for such an outlet, and merger_hints stays off.
  bool interleave = false;
  // With interleave: the target words a read keeps together, a power of two. Above one a counter
  // runs over the words entering the response stage from each packet's first: a word inside such a
  // block leaves without waiting for the next, and a segment ends only at a block's last word (or a
  // pause after it). The target must alternate IDs only there.
  unsigned interleave_beats = 1;
  // With interleave, for a target that never alternates IDs (read interleave size 0): no response
  // stage and no segments. The merge's word is the response word, a read keeps the merge to its
  // last beat, and a read's beats name it by RID as with interleave.
  bool response_direct = false;
  // The address bits of the widest response word a run gathers to (message.hpp point 18): the
  // outlet marks where a run ends; zero without runs.
  unsigned response_run_bits = 0;
  // AMBA 3 AXI ports (IHI0022B): AWLOCK and ARLOCK are two bits (0b01 exclusive, never locked) and
  // W carries WID, the AWID of the beat's burst; with several bursts in flight each W entry beat
  // keeps its own WID.
  bool axi3 = false;
  // Target flows, 1..32: above one the flow of the mapping holding a burst's start
  // (AddressMapping::flow) occupies the lowest bit_width(flows - 1) bits of its port AxID.
  // The encoded sequence occupies the remaining bits. A B or R names its burst's flow.
  unsigned flows = 1;
  // WRAP read alignment (with start_address, neither merged nor admitted): a read WRAP whose start
  // address is not on a word (a packed narrow WRAP, Axi4PacketWindow::align_wrap) goes out at its
  // start word; its first beat answers the lanes from the start address up and holds the lanes
  // below, which answer after the target's last beat as the packet's last word, RREADY low
  // meanwhile. Each read descriptor owns its saved word until its final response is accepted.
  // Different target IDs can retain saved words concurrently.
  bool align_wrap = false;
};
// AXI ID width at the target port: port_id_bits, else id_bits plus the source
// index bits when more than one burst is in flight, plus the flow bits.
unsigned axi4_outlet_id_bits(const Axi4OutletProfile &profile);
// Payload bits of the (write, read) engines of an outlet.
std::pair<unsigned, unsigned> axi4_outlet_outstanding_bits(const Axi4OutletProfile &profile);

Result<NodeHardware> lower_axi4_outlet(const Axi4OutletProfile &profile);

} // namespace nocloom::detail
