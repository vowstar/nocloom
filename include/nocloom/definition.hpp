#pragma once

#include "nocloom/result.hpp"
#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace nocloom {
// Apb3 and Apb2 are dialects of the APB family: Apb3 keeps PREADY and PSLVERR
// but has no PPROT or PSTRB; Apb2 completes every access in a fixed
// zero-wait-state cycle and has no PREADY, PSLVERR, PPROT or PSTRB. Initiators
// may be Apb5, Apb4 or Apb3 (single transfers, 8, 16 or 32 data bits in a 32-bit
// network); Apb2 is never a valid initiator protocol. Axi4 is the full AXI4 burst profile. AhbLite
// is AMBA 3 AHB-Lite: a target outlet, or an initiator's subordinate port. Axi3 is an AMBA 3 AXI
// port of an initiator or a target: AXI4 bursts of at most 16 beats with WID and a two-bit AxLOCK.
// Axi5 is an AMBA AXI5 port (IHI 0022 Issue L) with AXI4 bursts and the properties of its
// Axi5Properties object.
enum class Protocol {
  Apb4,
  Axi4Lite,
  Apb3,
  Apb2,
  Axi4,
  AhbLite,
  Axi3,
  Ahb5,
  Apb5,
  Wishbone,
  TlUl,
  TlUh,
  Axi5
};
namespace detail {
// Internal. The name of each protocol in a definition file, in the order of the manual. Every
// protocol serves a target; initiator is false for a protocol that does not serve an initiator.
struct ProtocolName {
  Protocol protocol;
  const char *name;
  bool initiator;
};
inline constexpr ProtocolName protocol_names[] = {
    {Protocol::Axi5, "axi5", true},         {Protocol::Axi4, "axi4", true},
    {Protocol::Axi3, "axi3", true},         {Protocol::Axi4Lite, "axi4_lite", true},
    {Protocol::Apb5, "apb5", true},         {Protocol::Apb4, "apb4", true},
    {Protocol::Apb3, "apb3", true},         {Protocol::Apb2, "apb2", false},
    {Protocol::AhbLite, "ahb_lite", true},  {Protocol::Ahb5, "ahb5", true},
    {Protocol::Wishbone, "wishbone", true}, {Protocol::TlUl, "tl_ul", true},
    {Protocol::TlUh, "tl_uh", true}};
// The protocol names that an initiator, or a target, accepts, as in "axi4, axi3 or apb4".
inline std::string supported_protocols(bool initiator) {
  std::string list;
  for (const auto &p : protocol_names)
    if (p.initiator || !initiator)
      list += (list.empty() ? "" : ", ") + std::string(p.name);
  return list.replace(list.rfind(','), 1, " or");
}
} // namespace detail
enum class AddressUnit { Byte, Word };
enum class WishboneRetry { Error };
enum class WishboneCycles { Classic, Pipelined };
// Each endpoint zone has its own gate by default. Both zones must be gated to share one gate.
struct EndpointClockGates {
  bool protocol = true;
  bool transport = true;
  bool single_gate = false;
  bool operator==(const EndpointClockGates &) const = default;
};
// Which flip-flops get a reset pin. All (the default) resets every flip-flop; Minimal resets only
// the control ones, and a clock crossing's payload slots and receive register then keep their
// value through a reset. A crossing's reset-handshake Gray counts have no reset pin in either
// mode: they clear synchronously while their side's handshake enable is low, so they never change
// asynchronously with the announce the other side samples (see "Clocks and resets").
enum class ResetCoverage { All, Minimal };
enum class Backend { Verilog2001 };
// How a register with a reset pin clears. Asynchronous (the default): at once while its reset is
// low. Synchronous: on a rising edge of its clock while its reset is low, synchronizer and reset
// release registers included; every clock gate then also passes its clock while its domain is in
// reset, and every clock must run while its reset is low (see "Clocks and resets").
enum class RegisterReset { Asynchronous, Synchronous };
// Arbitration of each switch output. FixedPriority requires a complete per-output list.
// LevelRoundRobin keeps one position per scheduling level.
enum class ArbitrationPolicy { RoundRobin, Fifo, RotatingPriority, FixedPriority, LevelRoundRobin };
// Endpoint direction. A read-only endpoint has no AW/W/B ports, a write-only one no AR/R ports.
enum class Access { ReadWrite, ReadOnly, WriteOnly };
// Source narrow burst handling. Merge (the default, also written by leaving
// the field out) merges narrow beats into words whatever their AxCACHE: with
// per-target data widths, an AXI4 target wider than a burst's beats merges
// them into target words. MergeModifiable (AXI4 initiators only) sends
// each beat of an INCR narrower than the source whose AxCACHE[1] and AxLOCK
// are clear as its own packet (an exclusive burst is not split into single
// beats) and merges the other bursts like Merge.
enum class NarrowBursts { Merge, MergeModifiable };
// FIXED bursts at a dedicated APB4, APB3, APB2, AXI4-Lite or AHB
// target, which has no fixed-address burst of its own. Reject answers SLVERR
// on every beat without target access. Single serves each beat as its own
// single transfer at the repeated address with the beat's size and strobes:
// every read beat returns its own result, and a write answers one B that
// fails if any beat failed.
enum class FixedBursts { Reject, Single };
// Exclusive access (AXI A7.2) at a target. None: the target does not
// support exclusive access; an AXI4 exclusive burst to it is performed as a
// normal access and answered OKAY, never EXOKAY (A7.2.3, A7.2.5), so an
// exclusive write always updates the target and reports failure. Network: a
// monitor in the target's outlet records exclusive reads by initiator and AxID
// or by Target::exclusive_owner_flags and lets an exclusive write reach the
// target only while its reservation holds (any target protocol). Target: an
// AXI4 or AXI3 target with its own monitor receives AxLOCK, and an exclusive access
// carries those flags as its target ID.
enum class ExclusiveAccess { None, Network, Target };
// The reservations kept by a network monitor.
struct ExclusiveMonitorOptions {
  // 1 to min(16, owner values). Absent uses that upper bound.
  std::optional<unsigned> reservations = std::nullopt;
  // Reservation alignment in bytes, a power of two from 1 to 128. Default 1.
  unsigned granule_bytes = 1;
  bool operator==(const ExclusiveMonitorOptions &) const = default;
};

// Target attribute mapping. Each target attribute bit takes a
// constant or a flag the source burst carries, optionally inverted. Flags:
// bufferable AxCACHE[0], modifiable AxCACHE[1], allocate AxCACHE[2] on reads
// and AxCACHE[3] on writes, lookup AxCACHE[2] | AxCACHE[3], privileged
// AxPROT[0], nonsecure AxPROT[1], instruction AxPROT[2]. User drives a network
// user flag from bit `index` of the initiator's AxUSER or PAUSER (Initiator::user_flags only).
enum class AttributeSource {
  Zero,
  One,
  Bufferable,
  Modifiable,
  Allocate,
  Lookup,
  Privileged,
  Nonsecure,
  Instruction,
  User
};
struct AttributeBit {
  AttributeSource source = AttributeSource::Zero;
  bool invert = false;
  unsigned index = 0; // the AxUSER bit of AttributeSource::User

  bool operator==(const AttributeBit &) const = default;
};
// Bit i of each array drives bit i of the target signal: AXI4 ARCACHE and
// AWCACHE, AXI4 and AXI4-Lite AxPROT or APB4 PPROT, and the AHB mapping m with
// HPROT = {m[3], m[2], m[1], ~m[0]}. A target uses the arrays of its protocol.
struct TargetAttributes {
  std::array<AttributeBit, 4> read_cache{};
  std::array<AttributeBit, 4> write_cache{};
  std::array<AttributeBit, 3> prot{};
  std::array<AttributeBit, 4> hprot{};
  // The conventional AMBA mapping, and all-zero constants.
  static TargetAttributes amba() {
    using S = AttributeSource;
    TargetAttributes t;
    t.read_cache = {{{S::Bufferable}, {S::Modifiable}, {S::Allocate}, {S::Lookup}}};
    t.write_cache = {{{S::Bufferable}, {S::Modifiable}, {S::Lookup}, {S::Allocate}}};
    t.prot = {{{S::Privileged}, {S::Nonsecure}, {S::Instruction}}};
    t.hprot = {{{S::Instruction}, {S::Privileged}, {S::Bufferable}, {S::Modifiable}}};
    return t;
  }
  static TargetAttributes zero() { return {}; }
  bool operator==(const TargetAttributes &) const = default;
};
// One AxUSER bit of a target: the network user flag named `flag`, or the
// constant `value` when flag is empty, optionally inverted.
struct UserBit {
  std::string flag;
  bool value = false;
  bool invert = false;
  bool operator==(const UserBit &) const = default;
};

struct AddressWindow {
  // Global byte address interval [base, base + size). Validation checks addition overflow.
  std::uint64_t base = 0;
  std::uint64_t size = 0;
  bool operator==(const AddressWindow &) const = default;
};
// One address mapping of a target: the global interval [base, base + size) reaches the target at
// local byte address local_address + (address - base). A mapping is whole 4 KiB pages (base and
// local_address 4 KiB aligned), or at a dedicated target a power-of-two sub-page of at least four
// bytes with base and local_address aligned to its size.
struct AddressMapping {
  std::uint64_t base = 0;
  std::uint64_t size = 0;
  std::uint64_t local_address = 0;
  // The mode condition (docs/address-map.md#address-selectors): the mapping decodes only while
  // every named address selector has the given value. Empty decodes in every mode. A conditional
  // mapping is whole pages.
  std::map<std::string, bool> when = {};
  // The target flow the mapping reaches (Target::flows), 0 for a target of one flow.
  unsigned flow = 0;
  AddressWindow window() const { return {base, size}; }
  bool operator==(const AddressMapping &) const = default;
};
// An address input port, 1 to 32 bits wide; the name is the port name as written. Every source
// whose address decode depends on it is in clock_domain, which samples it.
struct AddressInput {
  std::string name;
  unsigned width = 1;
  std::string clock_domain;
  bool operator==(const AddressInput &) const = default;
};
// An address selector: true while every listed address input equals its value. The inputs it does
// not list do not matter; it lists at least one.
struct AddressSelector {
  std::string name;
  std::map<std::string, std::uint32_t> equals;
  bool operator==(const AddressSelector &) const = default;
};
// An active-low reset input port; the name is the port name as written.
struct ResetPort {
  std::string name;
  bool operator==(const ResetPort &) const = default;
};
// A test mode input port, active high; the name is the port name as written. While it is high,
// every clock gate of the domains that name it passes its clock.
struct TestPort {
  std::string name;
  bool operator==(const TestPort &) const = default;
};
// An active-high input marking the rising edges a domain uses.
// The name is the port name as written. Every enable port serves at least one domain.
struct EnablePort {
  std::string name;
  bool operator==(const EnablePort &) const = default;
};
// An ungate input port, active high; the name is the port name as written. While it is high,
// every clock gate of the domains that name it passes its clock.
struct UngatePort {
  std::string name;
  bool operator==(const UngatePort &) const = default;
};
// A library cell bound to a generated cell wrapper by module and pin names. Every other input
// pin of the cell is tied to a constant (tie: pin -> value); unlisted pins stay unconnected.
// clock_gate: a positive-edge integrated clock gating cell with an active-high enable and an
// optional test pin (without one, enable receives enable | test). synchronizer: a flip-flop
// (stages 1) the wrapper chains, or an integrated synchronizer of stages 2..4 used as is for
// chains of that length; its reset pin (active low unless reset_active_low is false) clears it.
struct ClockGateCell {
  std::string module, clock, enable;
  std::optional<std::string> test = std::nullopt;
  std::string output;
  std::map<std::string, bool> tie = {};
  bool operator==(const ClockGateCell &) const = default;
};
struct SynchronizerBinding {
  std::string module;
  unsigned stages = 1;
  std::string clock, data, reset;
  bool reset_active_low = true;
  std::string output;
  std::map<std::string, bool> tie = {};
  bool operator==(const SynchronizerBinding &) const = default;
};
struct TechnologyCells {
  std::optional<ClockGateCell> clock_gate = std::nullopt;
  std::optional<SynchronizerBinding> synchronizer = std::nullopt;
  bool operator==(const TechnologyCells &) const = default;
};
// A physical clock root shared by explicitly related domains. Equal periods do not imply a root.
struct ClockRoot {
  std::string name;
  std::string clock_port;
  std::optional<std::uint64_t> period_ps = std::nullopt;
  bool operator==(const ClockRoot &) const = default;
};
// One clock domain: its clock input port and the reset port that resets its
// flip-flops (several domains may share one reset port). Port names are used
// as written.
struct ClockDomain {
  std::string name;
  std::string clock_port;
  std::string reset_port;
  // The test port of the domain's clock gates; required when the network gates this domain,
  // which it does by default (Definition::clock_gating).
  std::optional<std::string> test_port = std::nullopt;
  // Optional whole period in ps for clocked graphs. A fixed chain takes its exact period.
  // A chain with an enable port takes a minimum edge spacing at least as large as the
  // bound from its root and ancestor declarations. Planning uses this value when given.
  std::optional<std::uint64_t> period_ps = std::nullopt;
  // False disables automatic region clock gates in this domain.
  bool gated = true;
  // Use one of every ratio parent edges, or clock_port edges without a parent.
  std::optional<unsigned> ratio = std::nullopt;
  // Use the rising edges marked by this input. Absent by default. Mutually exclusive with ratio.
  std::optional<std::string> enable_port = std::nullopt;
  // With clock_roots, every domain names a physical root.
  // A domain with ratio or enable_port also names a parent domain in the same root.
  std::optional<std::string> clock_root = std::nullopt, edge_parent = std::nullopt;
  // An optional active-high input that holds this domain's clock gates open.
  std::optional<std::string> ungate_port = std::nullopt;
  // By default, two registers on the input clock synchronize release of reset_port.
  // False drives the domain directly. The system must meet recovery and removal times.
  bool reset_synchronizer = true;
  bool operator==(const ClockDomain &) const = default;
};
// Asynchronous Gray-count FIFO options. depth is 2..10; synchronizer_stages is 2..4.
struct AsyncClockCrossing {
  std::optional<unsigned> depth = std::nullopt;
  std::optional<unsigned> synchronizer_stages = std::nullopt;
  std::optional<bool> reset_handshake = std::nullopt;
  bool operator==(const AsyncClockCrossing &) const = default;
};
// Related-clock buffered-count options. depth is 2..4; fast requires depth 2 and removes only
// the local counter-return register, not the data buffer.
struct SyncClockCrossing {
  std::optional<unsigned> depth = std::nullopt;
  bool fast = false;
  bool operator==(const SyncClockCrossing &) const = default;
};
// Common-edge adapter options. The slow-side isolation pipe defaults on.
struct EdgeAlignedCrossing {
  bool slow_pipeline = true;
  bool operator==(const EdgeAlignedCrossing &) const = default;
};
using ClockCrossingOptions =
    std::variant<AsyncClockCrossing, SyncClockCrossing, EdgeAlignedCrossing>;
// Overrides for every crossing whose sender is from_domain and whose receiver is to_domain.
// The default options are asynchronous; related modes require an explicit common-root plan.
struct ClockCrossing {
  std::string from_domain;
  std::string to_domain;
  ClockCrossingOptions options = AsyncClockCrossing{};
  ClockCrossing(std::string from, std::string to, ClockCrossingOptions mode = AsyncClockCrossing{})
      : from_domain(std::move(from)), to_domain(std::move(to)), options(std::move(mode)) {}
  ClockCrossing(std::string from, std::string to, std::optional<unsigned> depth,
                std::optional<unsigned> stages = std::nullopt,
                std::optional<bool> reset_handshake = std::nullopt)
      : ClockCrossing(std::move(from), std::move(to),
                      AsyncClockCrossing{depth, stages, reset_handshake}) {}
  bool operator==(const ClockCrossing &) const = default;
};
// Fixed network scheduling levels, 0..7; larger values have higher priority.
struct QosLevels {
  unsigned read = 0;
  unsigned write = 0;
  bool operator==(const QosLevels &) const = default;
};
// A source's bandwidth budget. Alone it holds new transfers back while the source has used more
// than burst_bytes beyond its rate; a started transfer completes. The budget counts request bytes.
// Set one rate field. The default rate is zero and never recovers.
struct QosShaper {
  std::optional<unsigned> bytes_per_1024_cycles{};  // 0..65535
  unsigned burst_bytes = 0;                         // 0..65535
  std::optional<unsigned> bytes_per_32768_cycles{}; // 0..2097151
  bool operator==(const QosShaper &) const = default;
};
// With a shaper: the budget picks the scheduling level instead of holding transfers back. A source
// that has used at most threshold_percent of burst_bytes takes under_level, otherwise over_level.
struct QosScheduler {
  unsigned under_level = 0;       // 0..7
  unsigned over_level = 0;        // 0..7
  unsigned threshold_percent = 0; // 0..100
  bool operator==(const QosScheduler &) const = default;
};
// A source's scheduling and bandwidth. None of it consumes source AxQOS or drives target AxQOS,
// except axqos_map.
struct InitiatorQos {
  QosLevels levels{};
  // Absent: requests enter the network without a bandwidth budget.
  std::optional<QosShaper> shaper = std::nullopt;
  // Needs a shaper and excludes fixed levels.
  std::optional<QosScheduler> scheduler = std::nullopt;
  // An axi4 or axi3 initiator's AxQOS map: entry q is the scheduling level (0..7) of a burst
  // whose AWQOS or ARQOS is q. Its length (2, 4, 8 or 16) sets the AWQOS and ARQOS port width
  // (1..4 bits) and its entries never decrease. Empty: no AxQOS ports. It excludes the fixed
  // levels, the shaper and the scheduler.
  std::vector<unsigned> axqos_map{};
  // Whether the budget holds transfers back, or picks levels.
  bool holds() const { return shaper.has_value() && !scheduler.has_value(); }
  bool picks() const { return scheduler.has_value(); }
  bool operator==(const InitiatorQos &) const = default;
};
// None wires through. Forward registers valid and payload. Backward registers ready.
// Full registers both with two slots. Half uses one slot and transfers every two clocks.
enum class PipeSlice { None, Forward, Backward, Full, Half };

// A pipe clocked at one end of a link or at an endpoint.
struct LinkPipeline {
  PipeSlice slice = PipeSlice::None;
  // A static input that bypasses the forward slot. Empty means no bypass input.
  std::string bypass_port{};
  // False uses the region clock. True keeps the slice on the domain clock.
  bool ungated = false;
  constexpr bool forward_stage() const {
    return slice == PipeSlice::Forward || slice == PipeSlice::Full || slice == PipeSlice::Half;
  }
  constexpr bool backward_stage() const {
    return slice == PipeSlice::Backward || slice == PipeSlice::Full || slice == PipeSlice::Half;
  }
  constexpr unsigned slot_count() const {
    return slice == PipeSlice::Full ? 2U : slice == PipeSlice::None ? 0U : 1U;
  }
  bool operator==(const LinkPipeline &) const = default;
};
// Pipe positions in an initiator endpoint.
struct InitiatorPipes {
  // The default registers AWREADY, WREADY and ARREADY.
  LinkPipeline request{.slice = PipeSlice::Backward};
  // One pipe per direction after packet splitting and width conversion. No slice by default.
  LinkPipeline packets{};
  // An optional forward stage after read/write merge and burst admission.
  bool issue = false;
  // The default registers R and B in the initiator engine. Full also registers their ready inputs.
  LinkPipeline response{.slice = PipeSlice::Forward};
  bool operator==(const InitiatorPipes &) const = default;
};
// Pipe positions in a target endpoint.
struct TargetPipes {
  // Network exit. No slice by default.
  LinkPipeline arrival{};
  // An optional forward stage before the direction split.
  bool dispatch = false;
  // After response merge. No slice by default.
  LinkPipeline response{};
  // The default registers AW, W and AR in the outlet. Full also registers their ready inputs.
  LinkPipeline request{.slice = PipeSlice::Forward};
  bool operator==(const TargetPipes &) const = default;
};
// A closed interval of source AxIDs assigned to a declared route class.
struct IdRange {
  unsigned first = 0, last = 0;
  std::string route_class;
  bool operator==(const IdRange &) const = default;
};
// AHB5 interface properties. A zero HMASTER width omits its port.
struct Ahb5Properties {
  unsigned hprot_width = 4;   // 4 or 7 protection bits
  bool hnonsec = false;       // include the HNONSEC port
  unsigned hmaster_width = 0; // 0..32, carried with the request
  bool exclusive = false;     // HEXCL and HEXOKAY are unsupported
  unsigned hauser_width = 0, hwuser_width = 0, hruser_width = 0, hbuser_width = 0;
  bool operator==(const Ahb5Properties &) const = default;
};
// AXI5 interface properties of an axi5 endpoint (IHI 0022 Issue L, table B2.4). The defaults add
// no port, and an axi5 endpoint without the object takes them. docs/axi5.md maps each field to its
// property name and states which values the network accepts.
enum class Axi5Mpam { None, PartId9, PartId12 };
enum class Axi5Tagging { None, Basic, Simplified, Standard };
enum class Axi5InterfaceCheck { None, Data, All };
enum class Axi5Untranslated { None, V1, V2, V3, V4 };
enum class Axi5CacheStash { None, Basic, Full };
enum class Axi5Dvm { None, Receiver };
enum class Axi5FlowControl { Ready, Credited };
struct Axi5Properties {
  // Request attributes that go with each packet to the target.
  Axi5Mpam mpam = Axi5Mpam::None;
  unsigned subsystem_id_width = 0; // 0..8
  // The AxSUBSYSID value of an initiator without the port (subsystem_id_width 0), 0..255.
  std::optional<unsigned> subsystem_id = std::nullopt;
  bool nsaid = false, pbha = false, trace = false, poison = false;
  // Values that an initiator stores and returns with the response.
  bool unique_id = false;
  unsigned write_loop_width = 0, read_loop_width = 0; // 0..8
  // Protection and memory attribute ports.
  bool prot = true, priv_inst = false, cache = true;
  unsigned pas_width = 0; // 0..3
  // Shareable transactions. Absent takes the value of the protocol: false and 0 on axi5.
  std::optional<bool> shareable = std::nullopt;
  std::optional<unsigned> write_snoop_width = std::nullopt, read_snoop_width = std::nullopt;
  unsigned cache_line_bytes = 64;
  // Constraints that the validator checks.
  bool consistent_decerr = false, regular_only = false, wrap_line_only = false;
  bool no_fixed_bursts = false, device_normal_independence = false;
  unsigned max_transaction_bytes = 4096;
  // Properties that the network supports only at the default value.
  bool wakeup = false, qos_accept = false, busy = false, read_chunking = false, atomic = false;
  Axi5Tagging mte = Axi5Tagging::None;
  Axi5InterfaceCheck interface_check = Axi5InterfaceCheck::None;
  Axi5Untranslated untranslated = Axi5Untranslated::None;
  bool rme = false, gdi = false, mec = false;
  Axi5CacheStash cache_stash = Axi5CacheStash::None;
  bool cmo_on_read = false, cmo_on_write = false, write_plus_cmo = false;
  bool persist_cmo = false, storage_cmo = false;
  bool prefetch = false, write_zero = false, write_deferrable = false;
  bool write_no_snoop_full = false, invalidate_hint = false, deallocation = false;
  bool shareable_cache = false, coherency_connection = false, act = false;
  Axi5Dvm dvm = Axi5Dvm::None;
  Axi5FlowControl flow_control = Axi5FlowControl::Ready;
  bool operator==(const Axi5Properties &) const = default;
};
struct Initiator {
  std::string name;
  std::string clock_domain;
  // Absent inherits the global address width.
  std::optional<unsigned> address_width = std::nullopt;
  // Required: axi4, axi3, a single-beat axi4_lite, apb5, apb4 or apb3 ingress, an ahb_lite
  // (AHB-Lite) or ahb5 ingress, or wishbone, tl_ul or tl_uh. An axi3 initiator follows the axi4
  // rules with len_width 0..4. An APB initiator is 8, 16 or 32 bits wide in a 32-bit network and
  // sets none of id_width, len_width, access, narrow_bursts or max_outstanding above one. An AHB
  // initiator is 32 to 1024 bits wide under the AXI4 width rules and sets none of them either.
  std::optional<Protocol> protocol = std::nullopt;
  // Required. In a 32-bit network an AXI4 source may be 64 to
  // 1024 bits wide: converted to 32 bits, or, once any target differs from the
  // network width, attached at its own width.
  std::optional<unsigned> data_width = std::nullopt;
  // AXI4 and AXI3 sources only: the AxID port width, 0..16 (absent 4), and the AxLEN
  // port width, 0..8 (AXI3 0..4; absent 4). The network widens its own ID and length
  // fields to the widest source and the longest packet.
  std::optional<unsigned> id_width = std::nullopt;
  std::optional<unsigned> len_width = std::nullopt;
  std::optional<Access> access = std::nullopt;
  // Absent means NarrowBursts::Merge.
  std::optional<NarrowBursts> narrow_bursts = std::nullopt;
  // Bursts this source keeps in flight, reads and writes together, 1..64; absent
  // inherits transport.max_outstanding. Above one, the bursts of one AXI ID in
  // flight on a direction go to one target (or complete locally) and answer
  // in issue order; a burst whose ID is bound to another destination waits
  // until that ID has no burst in flight. Different IDs may go to different
  // targets and answer in any order. An AXI4-Lite source carries no AXI ID: all
  // its transactions of a direction share one ordering key, so they pipeline to
  // one destination and wait to change it. Combines with exclusive access
  // support, clocked graphs, packets and width conversion, including
  // per-target widths. A conversion that needs several pieces or a local
  // error drains earlier responses and excludes later packets until complete.
  // The bursts that this source splits into several packets, folds, or packs
  // from several beats share four packets in flight for reads and four for
  // writes. max_outstanding does not change that count
  // (docs/packets.md#split-packet-limit). Every AXI4 and
  // AXI4-Lite source also keeps at most n = max_outstanding transactions in
  // flight, reads and writes together: a burst takes one entry as its first
  // packet leaves and holds it until the last packet of its response arrives
  // back, however many packets the burst was cut into, and each single-beat
  // piece of a narrow-split burst takes one of its own. Bursts completed
  // locally take none.
  std::optional<unsigned> max_outstanding = std::nullopt;
  // Entries of the source's ID table, shared by reads and writes. A full AXI4 or AXI3 source has
  // {direction, AXI ID} keys and, with max_outstanding above one, accepts 1..min(max_outstanding,
  // directions * 2^ID width). An AXI4-Lite source has only direction keys and accepts
  // 1..min(max_outstanding, directions): one entry makes its reads and writes mutually exclusive;
  // two is its unconstrained bound. Absent inherits transport.id_table_entries, else the bound
  // (the table never fills). A transaction with a new key waits at the request merger while every
  // entry is busy, holding the other direction behind it. An entry frees as its last response
  // reaches the source.
  std::optional<unsigned> id_table_entries = std::nullopt;
  // AXI4, AXI3 or AHB sources to full AXI4/AXI3 targets no wider than the transport, or all 32-bit
  // APB, on one clock or placed on several. Burst capacities and packets keep their ordinary
  // limits; switch outputs arbitrate by the pressure the levels give their packets
  // (docs/scheduling.md#scheduling-pressure-rules). Each level is 0..7. All zero is the
  // default scheduling. A shaper or a scheduler needs a 32-bit apb5, apb4 or apb3, axi4 or axi3
  // initiator; an APB initiator with a scheduler has no packets or issue pipes.
  InitiatorQos qos{};
  InitiatorPipes pipes{};
  // The target that answers this source's decode misses, one it reaches. Absent: the target of
  // the smallest reached aligned block that starts inside the source's address space. Equal
  // sizes take the lowest block address, then the target name.
  std::optional<std::string> default_target = std::nullopt;
  // AXI4, AXI3 and AXI4-Lite sources: the AWUSER and ARUSER port width, 1..256; absent has no
  // port. APB5 uses PAUSER with 0..128 bits, absent or zero omits the port.
  std::optional<unsigned> user_width = std::nullopt;
  // With transport.user_flags, every source drives every flag (and no other): from an AxUSER or
  // PAUSER bit (AttributeSource::User), an AxCACHE or AxPROT flag of the burst, or a constant,
  // optionally inverted. An APB source carries AxCACHE 0 and its PPROT, an AHB source its HPROT
  // mapping.
  std::map<std::string, AttributeBit> user_flags = {};
  // With ErrorLogging::audit_flags, the driver of each flag, in the form of user_flags.
  std::map<std::string, AttributeBit> audit_flags = {};
  // AXI4, AXI3 and AXI4-Lite sources: the BUSER and RUSER port width, 1..256; absent has no port.
  // APB5 uses PBUSER with 0..16 bits, absent or zero omits the port.
  // They carry the low transport.response_user_bits of the response user field, zero above.
  std::optional<unsigned> response_user_width = std::nullopt;
  // APB5 PWUSER and PRUSER width. Zero omits both ports.
  unsigned data_user_width = 0;
  // Static source routing classes, at most 32. Their declaration indexes are source_path values
  // in connectivity and explicit routes. Empty means one direct class. With declared classes,
  // default_class is required and receives IDs outside id_ranges. Each class must be reachable.
  // Ranges of different classes must not overlap. A zero-bit ID permits only one class.
  std::vector<std::string> route_classes = {};
  std::optional<std::string> default_class = std::nullopt;
  std::vector<IdRange> id_ranges = {};
  std::optional<Ahb5Properties> ahb5 = std::nullopt;
  // Axi5 only. Absent takes the defaults.
  std::optional<Axi5Properties> axi5 = std::nullopt;
  // By default, the zones have separate gates. An override requires a gated domain.
  std::optional<EndpointClockGates> clock_gating = std::nullopt;
  // Wishbone requires an explicit address unit. Only targets accept retry: error.
  std::optional<AddressUnit> address_unit = std::nullopt;
  std::optional<WishboneRetry> retry = std::nullopt;
  std::optional<WishboneCycles> cycles = std::nullopt;
  // Optional CTI/BTE on classic Wishbone. Omission and false omit the ports.
  std::optional<bool> burst_tags = std::nullopt;
  bool operator==(const Initiator &) const = default;
  // TileLink source defaults to 4 bits on an initiator and 0 on a TL-UL target.
  // A zero width omits the port. Size defaults to 2 bits for TL-UL and 3 for TL-UH.
  // Sink defaults to 0 bits.
  std::optional<unsigned> source_width = std::nullopt;
  std::optional<unsigned> size_width = std::nullopt;
  std::optional<unsigned> sink_width = std::nullopt;
  // TL-UL permits 4 bytes. TL-UH initiators permit powers of two from 4 to 4096, default 64.
  std::optional<unsigned> max_transfer_bytes = std::nullopt;
  // AXI4, AXI3 and AXI4-Lite only. True returns SLVERR for every failure.
  // Omission and false preserve DECERR.
  std::optional<bool> slverr_only = std::nullopt;
};
// Targets: the outlet fails a target that leaves an issued handshake
// waiting for `cycles` consecutive target clock cycles. Without completion
// the failure is only reported: the bursts in flight and every later burst
// to the target never receive a response. With completion they answer SLVERR.
// TL-UH targets only latch expiry and keep accepting normal completions.
struct TargetTimeout {
  unsigned cycles = 0; // Target clock cycles, 16..1048576.
  bool completion = false;
  bool operator==(const TargetTimeout &) const = default;
};
struct Target {
  std::string name;
  std::string clock_domain;
  // At least one mapping. Where global intervals overlap the smaller aligned block decodes
  // (docs/address-map.md#target-mappings); local intervals of one target may overlap.
  std::vector<AddressMapping> mappings;
  // Local address port; every mapping's local interval must fit.
  std::optional<unsigned> address_width = std::nullopt;
  // Required: axi4, axi3 (an AXI4 target with len_width 4, WID and a two-bit AxLOCK), or a
  // dedicated ahb_lite, ahb5, apb5, apb4, apb3, apb2, axi4_lite, wishbone, tl_ul or tl_uh port.
  std::optional<Protocol> protocol = std::nullopt;
  // A read-only target never sees a write: the outlet absorbs it and
  // answers one SLVERR after W. A read-only AXI4 target has no AW/W/B ports.
  // A write-only target never sees a read: the outlet answers SLVERR with zero
  // data on every beat. A write-only AXI4 target has no AR/R ports.
  std::optional<Access> access = std::nullopt;
  // Required: APB 8, 16 or 32 bits; other targets 32, 64,
  // 128, 256, 512 or 1024 bits (AxSIZE 7,
  // 128 bytes, is the widest AXI beat). A width other than the network data
  // width needs an AXI4, AHB, 32/64-bit AXI4-Lite or 8/16-bit APB target (with
  // or without an explicit topology). APB targets use a 32-bit internal container and
  // split each beat into enabled physical words, gathering its read response.
  // Bursts narrower than an AXI4 or AHB target are merged into target words
  // (see NarrowBursts). A wider source is clamped to the target width.
  std::optional<unsigned> data_width = std::nullopt;
  // Required on AXI4, AXI4-Lite, APB4 and AHB targets (TargetAttributes::amba()
  // is the conventional mapping); APB3 and APB2 targets have none.
  std::optional<TargetAttributes> attributes = std::nullopt;
  // AXI4 and AXI3 targets only: the AxID port width, 0..31 (absent 4), and the AxLEN
  // port width, 4..8 (AXI3 4; absent 4). The length width bounds the target's packets
  // to 2^len_width target words (at most 4 KiB) and its WRAP to 16 words.
  std::optional<unsigned> id_width = std::nullopt;
  std::optional<unsigned> len_width = std::nullopt;
  // AXI4 targets only. True completes one read before another ID's data.
  // False allows interleaving. Absent derives the behavior from the interface.
  std::optional<bool> read_interleaving_disabled = std::nullopt;
  // AXI4 targets only. Target words kept together at an ID change, 1, 2, 4 or 8, up to 32 bytes.
  // An explicit block size enables interleaving unless read_interleaving_disabled is true.
  std::optional<unsigned> read_block_words = std::nullopt;
  // Dedicated APB, AXI4-Lite, AHB, Wishbone and TileLink targets; absent
  // means Reject. AXI4 targets receive FIXED bursts unchanged and reject this
  // field.
  std::optional<FixedBursts> fixed_bursts = std::nullopt;
  // AHB targets only; absent means true. False never drives HBURST
  // INCR: a packet of another length goes out as INCR16, INCR8, INCR4 and
  // SINGLE blocks that stay inside its 1 KiB boundary and the window. The
  // NONSEQ of the next block follows the last transfer of a block directly when
  // it is ready. Otherwise the bus shows IDLE. False makes every write SINGLE.
  std::optional<bool> undefined_length_bursts = std::nullopt;
  // AHB targets only; 1 or 2, absent means 1. One bit is the AMBA 3
  // AHB-Lite and AHB5 HRESP (0 OKAY, 1 ERROR). Two bits are the AMBA 2 AHB
  // HRESP[1:0] (OKAY, ERROR, RETRY, SPLIT) for AMBA 2 subordinates. ERROR
  // answers SLVERR. RETRY and SPLIT cancel the next address and fail the current
  // packet. The adapter does not retry.
  std::optional<unsigned> hresp_width = std::nullopt;
  // AXI4, AXI4-Lite, APB4, APB3 and AHB targets. After expiry the target
  // is failed until the common reset: no further access, and its bursts get
  // no response, or SLVERR with completion. TL-UH targets keep pending work
  // after report-only expiry and reject completion.
  std::optional<TargetTimeout> timeout = std::nullopt;
  // Bursts an AXI4 target keeps in flight, reads and writes combined in one
  // pool, 1..16; for a dedicated AXI4-Lite target, packets of one direction
  // in flight at once, 1..256 (a direction change waits for every earlier
  // packet's response; the words of a burst always issue back to back).
  // Absent means 1, and every other dedicated target keeps 1. An AXI4 burst
  // holds a pool entry from its head until its last response word leaves the
  // outlet; a head of either direction enters only while an entry is free.
  // Reads overlap whatever their target IDs (docs/transactions.md#target-identifiers), behind a
  // width merge only while they share one {source, AxID}; writes overlap whatever their target IDs.
  // Every reaching source has the target width in uniform-width mode; per-target width merges
  // retain K restoration contexts per direction. AXI4-Lite and APB sources reach it with AxID 0.
  // Packets each occupy one pool entry, independently of the source's original burst capacity and
  // its split-packet limit, which is four counted packets in flight for each direction
  // (docs/packets.md#split-packet-limit).
  std::optional<unsigned> max_outstanding = std::nullopt;
  // Exclusive access support; absent means ExclusiveAccess::None.
  // Network needs a read-write target; Target needs a read-write AXI4 or AXI3 target.
  // A target with exclusive access needs at
  // least one read-write AXI4 or AXI3 source that reaches it.
  std::optional<ExclusiveAccess> exclusive = std::nullopt;
  // Network monitors only. Absent uses ExclusiveMonitorOptions defaults.
  std::optional<ExclusiveMonitorOptions> exclusive_monitor = std::nullopt;
  // Distinct transport.user_flags naming the owner. The first name in lexical order is its low bit.
  // Empty uses the initiator and AxID for a network monitor. Target monitors require flags.
  std::vector<std::string> exclusive_owner_flags = {};
  // APB targets only. True permits consecutive SETUP/ACCESS pairs
  // when requests and response capacity are available; the packet capacity
  // stays one per direction. Absent or false preserves the default idle
  // cycle between accesses.
  std::optional<bool> apb_back_to_back = std::nullopt;
  // Every target protocol; absent means false. True marks reads (and writes) with side effects:
  // an AXI4 or AHB source sends an INCR burst with AxCACHE[1] and AxLOCK clear whose start lies
  // in this target as naturally aligned power-of-two blocks, one target access each, and
  // gathers the read responses back into its own beats.
  // TL-UH targets also use exact blocks for the WRAP tail and head runs.
  std::optional<bool> read_side_effects = std::nullopt;
  // AXI4, AXI3 and AXI4-Lite targets: the AWUSER and ARUSER port, bit i from user[i] (1..256
  // bits). APB5 uses PAUSER with 0..128 bits. Empty has no port. Every packet of a burst carries
  // the flags its source drove.
  std::vector<UserBit> user = {};
  // AXI4, AXI3 and AXI4-Lite targets: the BUSER and RUSER port width, 1..256; absent has no port
  // (the response user field is zero). A response packet carries the user of its first R beat, or
  // of the B, in the low transport.response_user_bits. APB5 uses PBUSER with 0..16
  // bits, absent or zero omits the port. Its packet carries the first completed access's PBUSER.
  // A timeout write response carries zero. Timeout read responses retain the packet's first value.
  std::optional<unsigned> response_user_width = std::nullopt;
  // APB5 PWUSER and PRUSER width. Zero omits both ports.
  unsigned data_user_width = 0;
  TargetPipes pipes{};
  // Target flows, 1..32, each reached by its own mappings (AddressMapping::flow, every flow at
  // least one) and taking its own explicit routes (ExchangeRoute::target_path). An apb4,
  // apb3, apb2, ahb_lite or axi4_lite target has one complete port set per flow; an axi4 or axi3
  // target carries the flow number in the lowest bits of its port AxID. Capacity and ordering stay
  // the target's.
  unsigned flows = 1;
  // The window of error logging's registers (docs/error-logging.md): this target has no ports of
  // its own. A configuration writes it as error_logging.registers; protocol stays apb4 here.
  bool log_registers = false;
  std::optional<Ahb5Properties> ahb5 = std::nullopt;
  // Axi5 only. Absent takes the defaults.
  std::optional<Axi5Properties> axi5 = std::nullopt;
  // By default, the zones have separate gates. An override requires a gated domain.
  std::optional<EndpointClockGates> clock_gating = std::nullopt;
  // Wishbone requires an explicit address unit. Only targets accept retry: error.
  std::optional<AddressUnit> address_unit = std::nullopt;
  std::optional<WishboneRetry> retry = std::nullopt;
  std::optional<WishboneCycles> cycles = std::nullopt;
  // Optional CTI/BTE on classic Wishbone. Omission and false omit the ports.
  std::optional<bool> burst_tags = std::nullopt;
  bool operator==(const Target &) const = default;
  // TL-UL targets default source to zero. TL-UH derives it from 1..16 packet contexts.
  // Explicit source width can widen that encoding to 16 bits. Sink defaults to zero.
  // A zero width omits the port. Size defaults to 2 bits for TL-UL and 3 for TL-UH.
  std::optional<unsigned> source_width = std::nullopt;
  std::optional<unsigned> size_width = std::nullopt;
  std::optional<unsigned> sink_width = std::nullopt;
  // TL-UL accepts 4 bytes. TL-UH accepts powers of two from 4 to 4096, default 64.
  std::optional<unsigned> max_transfer_bytes = std::nullopt;
};
struct TreeNode {
  std::string name;
  std::optional<std::string> parent;
  std::vector<std::string> targets;
  std::optional<std::string> clock_domain = std::nullopt;
};

// Arbitration of one physical output of a switch node. output names
// the output as "node/<successor>" or "endpoint/<target or initiator>"; it must
// have at least two candidates. policy is RoundRobin, Fifo, RotatingPriority
// or FixedPriority. priority (FixedPriority only, and required there) lists
// every candidate of the output once, highest priority first: "node/<name>"
// for a predecessor switch, "endpoint/<name>" for an attached entry. The
// setting applies to the output's write and read channels alike.
struct OutputArbitration {
  std::string output;
  ArbitrationPolicy policy = ArbitrationPolicy::RoundRobin;
  std::vector<std::string> priority;
  bool operator==(const OutputArbitration &) const = default;
};
// Directional exchange objects. Node names are scoped to a graph.
struct ExchangeNode {
  std::string name;
  std::optional<unsigned> transport_word_bytes = std::nullopt;
  // Explicitly clocked graphs bind every switch to a declared domain.
  std::optional<std::string> clock_domain = std::nullopt;
  // Per-output arbitration overrides; the other outputs follow the default.
  std::vector<OutputArbitration> arbitration = {};
  bool operator==(const ExchangeNode &) const = default;
};
// transport_word_bytes absent takes the narrower end word that still carries
// the slower end's bandwidth (word times 1 / period_ps); ends without both
// periods count as equally fast.
struct ExchangeLink {
  std::string from, to;
  std::optional<unsigned> transport_word_bytes = std::nullopt;
  // pipeline: at the receiving switch, after the link width conversion and any
  // clock crossing; sender_pipeline: at the sending switch, before both.
  LinkPipeline pipeline{};
  LinkPipeline sender_pipeline{};
  bool operator==(const ExchangeLink &) const = default;
};
struct ExchangeAttachment {
  std::string endpoint, node;
  // At the switch, which receives an entry and sends an exit.
  LinkPipeline pipeline{};
  bool operator==(const ExchangeAttachment &) const = default;
};
struct ExchangeGraph {
  std::vector<ExchangeNode> nodes;
  std::vector<ExchangeLink> links;
  std::vector<ExchangeAttachment> entries, exits;
  bool operator==(const ExchangeGraph &) const = default;
};
// The explicit route of one flow pair of a connected pair, keyed by its initiator, initiator flow,
// target and target flow. request lists the request graph nodes from the initiator's attachment
// node to the target's, response the response graph nodes from the target's attachment node to the
// initiator's; each follows declared links and enters a node it passes again by another link (the
// route waits decide whether that is legal). At least one is given; an absent one is the unique
// route of its graph.
struct ExchangeRoute {
  std::string initiator, target;
  std::vector<std::string> request = {}, response = {};
  // The initiator flow and the target flow the route carries (Initiator::route_classes,
  // Target::flows); each flow pair of a connected pair takes its own route entry or the unique
  // route of each graph.
  unsigned source_path = 0, target_path = 0;
  bool operator==(const ExchangeRoute &) const = default;
};
// An explicit topology describes only the physical graphs; Definition::connectivity
// decides which source-target pairs they carry. routes fixes the paths of some connected pairs;
// every other connected pair needs a unique route in each graph.
struct ExchangeTopology {
  ExchangeGraph request, response;
  std::vector<ExchangeRoute> routes = {};
  bool operator==(const ExchangeTopology &) const = default;
};
// Connectivity matrix: the source-target pairs the network carries.
// A source reaches only its listed targets. An unconnected flow pair is a decode miss,
// completed through the default target adapter without a target bus access. The result is
// DECERR at AXI initiators (SLVERR with slverr_only). all = true admits every pair and takes no
// pairs. Every initiator and every target must appear in at least one pair. An entry connects every
// pair of the source's flows (Initiator::route_classes) and the target's flows (Target::flows), or
// only those of source_path and target_path when given; several entries of one pair connect
// distinct flow pairs. A burst of a flow pair the matrix does not connect misses as above.
struct ConnectivityEntry {
  std::string source, target;
  std::optional<unsigned> source_path = std::nullopt, target_path = std::nullopt;
  bool operator==(const ConnectivityEntry &) const = default;
};
struct Connectivity {
  bool all = false;
  std::vector<ConnectivityEntry> pairs = {};
  bool operator==(const Connectivity &) const = default;
};
// Narrower packet limits of one connected source-target pair. Every AXI4
// source cuts its bursts into packets at the limits of the target a packet
// reaches (see docs/packets.md#bounded-packets). max_bytes and boundary_bytes are
// powers of two from the pair's alignment word (the wider of the two data
// widths, APB targets counting 4 bytes) to the pair's default; absent keeps
// the default, and at least one is set.
struct PacketLimit {
  std::string source, target;
  std::optional<std::uint64_t> max_bytes = std::nullopt;
  std::optional<std::uint64_t> boundary_bytes = std::nullopt;
  bool operator==(const PacketLimit &) const = default;
};
// Network options. max_outstanding: 1..64 bursts for each AXI4, AXI3 or AXI4-Lite source, reads
// and writes together, without its own value (and its shared pool of transactions in flight, see
// Initiator::max_outstanding); id_table_entries: the ID table entries of each AXI4, AXI3 or
// AXI4-Lite source with more than one outstanding transaction and without its own value (absent
// derives the bound, see Initiator::id_table_entries). data_width: the network width, 32 or 64
// bits. If absent, take the smallest endpoint data_width, capped at 64, counting APB engines as 32.
// APB endpoints require a 32-bit network. Targets of another width are AXI4, AHB, 32/64-bit
// AXI4-Lite or 8/16-bit APB targets. transport_word_bytes: 4, 8, 16 or 32; absent defaults to the
// network data width / 8. packet_limits narrows the packet limits of
// connected source-target pairs (see PacketLimit).
struct TransportOptions {
  std::optional<unsigned> data_width = std::nullopt;
  unsigned max_outstanding = 1;
  std::optional<unsigned> transport_word_bytes = std::nullopt;
  std::optional<unsigned> id_table_entries = std::nullopt;
  std::vector<PacketLimit> packet_limits = {};
  // Network user flags, 1..256 distinct names: each one bit of every request packet header, driven
  // by every source (Initiator::user_flags) and mapped onto target AxUSER bits (Target::user).
  std::vector<std::string> user_flags = {};
  // The response user field of every response packet, 0..256 bits (zero omits it).
  unsigned response_user_bits = 0;
  // Data user bits per data byte, 0..4. Zero omits the payload field.
  unsigned data_user_bits_per_byte = 0;
  // Use one internal failure class. All AXI initiators must set slverr_only and
  // no log may record classes. The default keeps distinct internal failure classes.
  bool compact_failure_responses = false;
  bool operator==(const TransportOptions &) const = default;
};
// Which errors a logger of an error log keeps, and what it does with them (docs/error-logging.md).
struct ErrorLogger {
  // Classes the logger skips: target, decode. Requires ErrorLogging::capture_class.
  std::vector<std::string> skip = {};
  // The values a record's audit flags and user flags must have; a flag not named may have any.
  std::map<std::string, bool> match = {};
  // A record leaves for the next logger always, or only unless this logger logged it.
  enum class PassOn : std::uint8_t { UnlessLogged, Always } pass_on = PassOn::UnlessLogged;
  // The bank's byte offset in the window; absent: the next free place after the banks before it.
  std::optional<std::uint64_t> offset = std::nullopt;
  bool operator==(const ErrorLogger &) const = default;
};
// A record of the first error of the targets it watches, read and cleared through a bank of
// registers.
struct ErrorLog {
  std::string name;
  // The data targets whose responses it watches; this order is their arbitration order.
  std::vector<std::string> watch;
  // The clock of its logger and bank; absent: the window's.
  std::optional<std::string> clock_domain = std::nullopt;
  // Records keep the whole address and every user flag.
  bool whole_record = false;
  // Its loggers in chain order; empty: one logger of the defaults.
  std::vector<ErrorLogger> loggers = {};
  // Bits per word on the log crossing: 8, 16 or 32.
  unsigned serial_word_bits = 32;
  bool operator==(const ErrorLog &) const = default;
};
struct ErrorLogging {
  // Flags each request carries from its source to its target for a record to show; they grant and
  // deny nothing. A record's word holds them in this order.
  std::vector<std::string> audit_flags = {};
  std::vector<ErrorLog> logs = {};
  // Every bank's revision register; absent: the generator version (major, minor and patch, one
  // byte each), so the RTL then changes with that version.
  std::optional<std::uint32_t> revision = std::nullopt;
  // Store the target or decode class in each record. False sets the class field to zero.
  bool capture_class = true;
  bool operator==(const ErrorLogging &) const = default;
};
// Definitions name each endpoint's protocol and data width. A network has 1..256
// initiators and targets. Without topology, it has one same-domain switch per direction.
// With a topology, each connected pair needs an explicit or unique directed route.
struct Definition {
  unsigned schema_version = 10;
  std::string name;
  unsigned address_width = 32;
  // Every reset port is used by at least one domain.
  std::vector<ResetPort> reset_ports;
  // A test port unused by any domain remains an unloaded module input.
  std::vector<TestPort> test_ports;
  std::vector<EnablePort> enable_ports;
  std::vector<UngatePort> ungate_ports;
  // Address inputs and the selectors over them that mapping conditions (AddressMapping::when)
  // name. Every input is compared by a selector, and every selector is named by a mapping.
  std::vector<AddressInput> address_inputs;
  std::vector<AddressSelector> address_selectors;
  // True enables automatic gates on switches and endpoints in gated domains.
  bool clock_gating = true;
  ResetCoverage reset_coverage = ResetCoverage::All;
  // Library cells behind the clock gate and synchronizer wrappers; unbound wrappers hold a
  // behavioral model.
  TechnologyCells cells = {};
  std::vector<ClockRoot> clock_roots;
  std::vector<ClockDomain> clock_domains;
  // Clocked graphs only: the register stages of every crossing synchronizer
  // chain, 2..4; absent means 2. Needs a crossing that takes it.
  std::optional<unsigned> synchronizer_stages = std::nullopt;
  // Clocked graphs only: per directed domain pair overrides.
  std::vector<ClockCrossing> clock_crossings;
  std::vector<Initiator> initiators;
  std::vector<Target> targets;
  // Default policy for every switch output without an override.
  ArbitrationPolicy arbitration = ArbitrationPolicy::RoundRobin;
  // Absent selects the TransportOptions defaults.
  std::optional<TransportOptions> transport;
  // Absent generates one switch per direction from connectivity.
  std::optional<ExchangeTopology> topology;
  // Required; Connectivity::all admits every pair.
  std::optional<Connectivity> connectivity;
  // Error logs on the one target with log_registers.
  ErrorLogging error_logging = {};
  // Field by field, in order: equal definitions generate the same bytes.
  bool operator==(const Definition &) const = default;
};
// The limits of the route cycle report (docs/network.md#route-cycle-report). A limit in its range
// changes the diagnostics of a rejected definition only.
struct ValidationOptions {
  // The cycles reported for each direction, 1 to 1024: any other value is rejected. A note tells
  // when a direction has more.
  unsigned cycle_limit = 4;
  // The routes listed for each wait of a cycle. 0 lists every route.
  unsigned contributor_limit = 8;
};
struct GenerationOptions {
  Backend backend = Backend::Verilog2001;
  RegisterReset register_reset = RegisterReset::Asynchronous;
  // Adds <top>.cdc.sdc (SDC 2.1 timing exceptions of every clock-domain crossing) and
  // <top>.cdc_preserve.txt (cells to keep through synthesis) and lists both in the bundle.
  // Requires a clocked network with at least one crossing. Off leaves every artifact unchanged.
  bool cdc_constraints = false;
  // Adds one address-window C header per initiator and a register header when error logs exist.
  bool c_header = false;
  ValidationOptions validation = {};
  // Adds JSON and CSV address tables for the initiators.
  bool address_table = false;
  // Adds an IP-XACT component with the default component identifier.
  bool ipxact = false;
};
using SourceMap = std::map<std::string, SourceLocation>;
struct ParsedDefinition {
  Definition definition;
  GenerationOptions options;
  SourceMap sources;
};
} // namespace nocloom
