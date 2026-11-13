#pragma once
#include "component.hpp"
#include "exclusive.hpp"
#include <nocloom/definition.hpp>
#include <optional>

namespace nocloom::detail {
// Internal admitted burst packets carry start_address and share a common reset with the target. APB
// and AXI4-Lite keep one packet per direction. No CDC. APB carries a 32-bit internal container and
// an 8/16/32-bit physical word, AXI4-Lite 32 or 64, and AHB data_bits. The mappings (definition.hpp
// AddressMapping, in base order, none decodes every address) are whole 4 KiB pages or power-of-two
// sub-pages of at least a word aligned to their size. With a sub-page mapping every beat is decoded
// against the exact mappings, so a beat outside them completes with DECERR and no target access.
// Each beat reaches the target at the local address of the mapping holding it, which address_bits
// narrows to the local port; transport_address_bits (zero means 32) sizes the packet and mapping
// checks.
struct BurstOutletProfile {
  std::string name;
  unsigned id_bits = 0;
  unsigned len_bits = 8;
  unsigned source_bits = 0;
  std::vector<AddressMapping> mappings;
  unsigned address_bits = 32;
  unsigned transport_address_bits = 0;
  // Arrival block of the outlet's global addresses (AddressBlock).
  AddressBlock arrival{};
  // The address bits of the widest response word a run gathers to (message.hpp point 18): the
  // outlet marks where a run ends; zero without runs.
  unsigned response_run_bits = 0;
  // APB dialect for lower_apb_burst_outlet: apb5, apb4, apb3 or apb2.
  Protocol dialect = Protocol::Apb4;
  // Mapped PPROT, AXI4-Lite AxPROT or AHB HPROT; absent carries AxPROT through
  // (AHB HPROT = {cache[1:0], prot[0], ~prot[2]}).
  std::optional<TargetAttributes> attributes = std::nullopt;
  // False answers every FIXED beat with SLVERR and no access. True serves each
  // FIXED beat as its own single transfer at the repeated address with that
  // beat's size and strobes; the write B merges the beat results, and an APB3
  // or APB2 partial-strobe beat fails alone without draining later beats.
  bool fixed_single = false;
  // Every write answers SLVERR without target access; reads are unchanged.
  bool read_only = false;
  // Every read answers SLVERR with zero data without target access; writes are unchanged.
  // The engine takes the first fragment of a packet of the refused direction one cycle after
  // it arrives.
  bool write_only = false;
  // AXI4-Lite target DECERR maps to result 1 on that read word. Other failures map to 2.
  // Writes retain the first failed word's result. False maps all target failures to 2.
  bool error_codes = false;
  // Zero builds no timer. Otherwise cycles of the watchdog, a power of two from 16 to 1048576
  // (Logic::watchdog) fail the target until the common reset. Later words answer SLVERR (after the
  // window decode) with timeout_completion, else nothing. Issued handshakes are held and late
  // completions discarded. A read packet already held finishes its transfers, data discarded.
  // No write data follows (an AHB write burst stays in BUSY). Nothing else starts.
  // nl_status_timed_out goes high. APB2 cannot stall.
  unsigned timeout_cycles = 0;
  bool timeout_completion = false;
  // AHB only. Writes use undefined INCR or SINGLE, so partial strobes cannot terminate a
  // fixed-length burst. Without undefined_length_bursts every write is SINGLE and reads use
  // INCR16, INCR8, INCR4 and SINGLE blocks bounded by the packet end, the 1 KiB boundary and the
  // window end. A packet keeps the bus across its blocks.
  // The next block's NONSEQ follows a block's last transfer directly when ready, and BUSY appears
  // only inside a block.
  bool complete_write_bursts = false;
  bool undefined_length_bursts = true;
  // AHB only: 1 (AHB-Lite/AHB5 OKAY, ERROR) or 2 (AMBA 2). ERROR fails the transfer. RETRY
  // and SPLIT cancel the next address and fail the current packet (no retry).
  unsigned hresp_width = 1;
  bool ahb5 = false;
  unsigned hmaster_bits = 0, ahb5_metadata_bits = 0, hprot_bits = 4;
  bool hnonsec = false;
  // Target word: 32 for APB; 32 or 64 for AXI4-Lite; 32, 64, 128, 256, 512 or 1024 for AHB
  // (AHB-Lite 6.2). A burst beat keeps its AxSIZE as HSIZE: a read packet of beats of at least 32
  // bits is one burst that steps HADDR by 2^AxSIZE, byte and halfword packets are SINGLE transfers,
  // and a write word is full when its strobes cover the beat's AxSIZE container.
  unsigned data_bits = 32;
  // Weft exclusive fields (message.hpp contract point 11). Without a monitor the outlet ignores
  // lock and never answers exclusive_okay. With exclusive_target network the outlet's monitor
  // (exclusive_monitor.hpp) judges every exclusive access: an exclusive read answers exclusive_okay
  // on each beat without error; an exclusive write without a matching reservation goes out with no
  // strobes and zero data (AXI4-Lite: one write per word with WSTRB zero; APB and AHB: no transfer)
  // and answers result 0 without exclusive_okay (AXI OKAY) whatever the target answers.
  bool exclusive = false;
  ExclusiveTarget exclusive_target{};
  // Weft response steering tag width (message.hpp point 12); every response
  // returns its request's tag. Zero omits the field.
  unsigned tag_bits = 0;
  unsigned user_bits = 0; // network user flags (message.hpp point 13)
  unsigned data_user_bits_per_byte = 0;
  unsigned response_user_bits = 0;
  // AxUSER or PAUSER bit i: a constant or AttributeSource::User naming a flag by index; empty has
  // no port.
  std::vector<AttributeBit> user{};
  unsigned data_user_port_bits = 0;
  unsigned response_user_port_bits = 0; // BUSER/RUSER or APB5 PBUSER inputs, absent at zero
  // APB physical word, independent of the 32-bit internal packet container.
  unsigned apb_data_bits = 32;
  // APB32 only: start another SETUP at a completed ACCESS when request and
  // response capacity are available. Packet capacity stays one per direction.
  bool apb_back_to_back = false;
  // APB only: target flows, 1..32, one complete APB port set each (`<signal>_<flow>` above one).
  // A transfer goes to the flow of the mapping holding its address (AddressMapping::flow): PSEL
  // and PENABLE rise on that set alone and its PREADY, PRDATA and PSLVERR complete it; the other
  // outputs drive every set alike.
  unsigned flows = 1;
  // AXI4-Lite only: packets of one direction in flight at once, 1..256.
  // Words issue back to back without waiting for B or R whatever the value;
  // a direction change still waits until every earlier packet is answered.
  unsigned max_outstanding = 1;
};
using AhbOutletProfile = BurstOutletProfile;
using ApbBurstOutletProfile = BurstOutletProfile;
// What an APB outlet stores, by its form. A serial outlet without a timer keeps one response per
// direction (single), so a packet's fields can live in the response entry from the head on, and a
// read's address carries the WRAP base. Of whole words (at_once), the bus is idle at every word
// taken: a write word issues in that cycle, and its address and lanes are not stored
// (write_in_entry without an exclusive monitor, which reads a held packet's range), and a read's
// mask follows from its beat. Without a sub-page window a packet's code is its
// first (packet_coded): a word owes a transfer only if the first word did, and its attributes stay
// on the bus.
struct ApbOutletForm {
  bool single, at_once, write_in_entry, packet_coded;
};
ApbOutletForm apb_outlet_form(const BurstOutletProfile &profile);
// The bits an APB outlet stores of PADDR: only a transfer loads it, and a transfer's address is
// inside a mapping, so those that hold every local address of the mappings (all without mappings).
unsigned apb_bus_address_bits(const BurstOutletProfile &profile);
// APB4 uses aligned physical word accesses per enabled beat, preserving byte strobes together.
// FIXED bursts complete with SLVERR and no access unless fixed_single is set. Reads return only the
// requested lanes. Zero-strobe writes complete without selecting the target. APB and AXI4-Lite
// share one packet engine. By default every transfer starts from IDLE: a word takes an idle, a
// SETUP and an ACCESS cycle plus wait states (a word reaching an idle bus with an empty buffer
// issues in its arrival cycle, so PSEL rises at the next edge). The write buffer takes the next
// word while the current one is on the bus; a read keeps its offered response apart from the
// current word, advances at the completion of its transfer and is issued only when that response
// has room.
// apb_back_to_back uses two read response slots and can start SETUP at the preceding ACCESS
// completion. APB3 and APB2 omit PSTRB and PPROT: a partial-strobe word answers SLVERR without
// access when that physical subword is partial; earlier completed subwords are not rolled back and
// later full words still execute and the packet accumulates SLVERR. A zero-strobe word is skipped.
// APB2 omits PREADY and PSLVERR; each access takes one SETUP and one ACCESS cycle.
Result<NodeHardware> lower_apb_burst_outlet(const ApbBurstOutletProfile &profile);
// Dedicated AXI4-Lite: one single-beat transaction per word, a zero-strobe word included (WSTRB
// zero), AW and W accepted independently. The words of a burst issue back to back without waiting
// for B or R, and up to max_outstanding packets of one direction stay in flight; a direction change
// waits for every earlier packet's response. A write packet answers one B after the last word's B.
// A response reaches the network in the cycle it forms while the response stage is empty.
// Nonzero BRESP/RRESP fail (see error_codes); FIXED bursts answer SLVERR without access unless
// fixed_single is set.
Result<NodeHardware> lower_axi_lite_outlet(const BurstOutletProfile &profile);
// Dedicated AHB outlet without byte enables on the bus: a write word whose strobes are its whole
// AxSIZE container, the start block of a one-beat or FIXED beat, or exactly one half of the target
// word is one transfer, any other word one byte transfer per strobe from its lowest lane; with
// fixed_single each FIXED beat is served on its own. A one-beat or FIXED read covers the exact
// suffix from its start address to the container end using largest-fitting aligned transfers;
// longer reads take their containers. The outlet is pipelined (the next address phase overlaps the
// current data phase), so a burst issues one transfer per HREADY cycle while its write words and
// response room last (two read responses, counting the read transfers on the bus). Three write
// packet contexts feed one word, kept until HREADY accepts the address phase of its last transfer
// (the next word is taken in that cycle); a separate B snapshot keeps its metadata and result
// unless the B leaves as its packet closes.
Result<NodeHardware> lower_ahb_outlet(const AhbOutletProfile &profile);
} // namespace nocloom::detail
