"""Contract table: the model's defaults and the rule IDs it judges.

Expected behavior follows the manuals, generated map contracts and AMBA specifications.
Each entry names its public source.
"""

# Response codes (AMBA AXI A3.4.4).
OKAY, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3

# Severity order used when one source response merges several target
# responses (a split burst): DECERR over SLVERR over OKAY.
SEVERITY = {OKAY: 0, EXOKAY: 0, SLVERR: 1, DECERR: 2}

CONTRACTS = {
    "AXI.ROUTE.LOCAL_OFFSET": dict(
        text="A target receives the source address minus its window base, once",
        source="map contract address_transform=target_local_byte_offset_once"),
    "AXI.ERR.UNMAPPED_DECERR": dict(
        text="An address outside every reachable window answers DECERR without target access",
        source="docs/address-map.md; AMBA AXI A3.4.4"),
    "AXI.ERR.READ_ZERO": dict(
        text="A failed read beat returns zero data", default=True,
        source="docs/protocols.md: error read data is zero"),
    "AXI.MERGE.SEVERITY": dict(
        text="A source response covering several target responses is the most severe one",
        source="docs/width-conversion.md#width-conversion: merged B, DECERR before SLVERR"),
    "AXI.LITE.WORD_ACCESS": dict(
        text="An AXI4-Lite target sees one single-word access per touched target word",
        source="docs/dedicated-targets.md#dedicated-apb-and-axi4-lite-targets; AMBA AXI4-Lite B1.1"),
    "AXI.LITE.SOURCE_WRITE": dict(
        text="An AXI4-Lite source takes AW and W in either order; its pending write holds "
             "the source write storage from the first of the two handshakes until its B is taken",
        source="docs/protocols.md#axi4-lite-initiators (AW and W accepted independently, one pending "
               "write); map network.storage acquire and release"),
    "AXI.TIMEOUT.EXACT": dict(
        text="A target timer fails the target after timeout_cycles without progress of the waiting direction",
        source="docs/timeout-and-progress.md#target-timeouts"),
    "AXI.EXCL.MONITOR": dict(
        text="Exclusive pairs succeed with EXOKAY only when no overlapping write intervened",
        source="AMBA AXI A7.2; docs/exclusive-access.md#exclusive-access"),
    "AXI.TARGET.ID_RULE": dict(
        text="A target AxID follows the generated map. Requests of "
             "one target ID keep their order, an exclusive access to a target monitor takes its "
             "owner flags",
        source="docs/transactions.md#target-identifiers; AMBA AXI A5.3"),
    "AXI.WIDTH.DOWNSIZE.PIECES": dict(
        text="A wide burst reaches a 32-bit target as the pieces of the cut rules",
        source="AMBA AXI A3.4.1 burst address rules; docs/width-conversion.md#width-conversion"),
    "AXI.WIDTH.FIXED_REFUSE": dict(
        text="A FIXED burst wider than the target in a target window answers SLVERR without target access",
        source="docs/width-conversion.md#width-conversion FIXED rule"),
    "AXI.QOS.LEVEL": dict(
        text="Fixed levels: the highest valid level is served first; a packet grant is held head to tail",
        source="map contract arbitration.qos.selection, holding"),
    "AXI.OUTSTANDING.REACH": dict(
        text="Configured outstanding capacity is reached at the target",
        source="docs/transactions.md"),
    "AXI.RESET.CANCEL": dict(
        text="Resets asserted together cancel every transaction in flight; nothing replays "
             "after them",
        source="docs/clocks-and-resets.md#reset-release"),
    "AXI.RESET.DOMAIN": dict(
        text="Each clock domain leaves reset through its own reset port; a domain in reset "
             "takes and forwards nothing, and traffic waiting for it completes after its release; "
             "with the reset handshake a domain reset alone makes no crossing replay a word",
        source="docs/clocks-and-resets.md#reset-release"),
    "AXI.W.UNSTROBED_ZERO": dict(
        text="Every write byte a target receives without its strobe is zero",
        source="map contract write_data_unstrobed_bytes=zero_at_every_target_cleared_at_source_capture"),
    "AXI.CDC.CROSSING": dict(
        text="Packets cross clock domains through the crossing FIFOs whole, in order and "
             "once, at any clock ratio; a stopped domain holds requests in local storage",
        source="docs/clocks-and-resets.md"),
    "AXI.CLOCK.GATE": dict(
        text="A gated switch or endpoint side takes, holds and forwards every word in the "
             "cycles it would without its gate: a closed gate opens in the cycle a word or an "
             "endpoint valid arrives and closes only while the side holds none; the test port "
             "held high changes no endpoint event",
        source="docs/clocks-and-resets.md#clock-gates"),
    "AXI.PIPE.SLOTS": dict(
        text="A link or endpoint pipe passes every word once and in order: a forward slot "
             "offers what it holds, a backward slot keeps a word while its output stalls",
        source="docs/pipes.md#link-pipes"),
    "AXI.ARB.POLICY": dict(
        text="Every arbitrated output grants by the policy the input states for it (per-output "
             "override, written default, or the default table), whole packets at a time",
        source="docs/arbitration.md#arbitration; map contract arbitration.outputs"),
    "AXI.RATE.BEATS": dict(
        text="Back-to-back bursts follow the documented beat rates (narrow side "
             "of a conversion, shared read and write links)",
        source="docs/width-conversion.md#beat-rates"),
    "AXI.SOURCE.MERGE": dict(
        text="A source's request at its port competes for the merger whatever its engine holds and "
             "stays chosen through a shortage; a burst's place in max_outstanding frees as its last "
             "response enters the source's R or B staging",
        source="docs/transactions.md#initiator-capacity"),
    "AXI.TARGET.R_INTERLEAVE": dict(
        text="An AXI4 target's R beats of different IDs may interleave beat by beat: each beat "
             "belongs to the oldest open read of its RID, and every source receives its own "
             "bursts' data, RLAST and codes",
        source="AMBA AXI A5.3 (read data interleaving); docs/transactions.md#target-capacity"),
    "AXI.ERR.READ_PACKET": dict(
        text="Each source read beat merges only its contributing target words, DECERR before "
             "SLVERR before OKAY, without waiting for unrelated suffix beats; local length "
             "descriptors have a separate tag and no target access",
        source="docs/address-map.md#error-responses"),
    "AXI.OUTLET.FRAMING": dict(
        text="A target outlet sends each beat once, with its AxID and WLAST, and returns "
             "every response beat framed (RLAST at the burst end)",
        source="AMBA AXI A3.3, A3.4; docs/protocols.md"),
    "AXI.UPSIZE.MERGE": dict(
        text="A narrow burst reaches a wider AXI4 target merged into target words: every "
             "strobed byte once, read data fanned out to each source beat",
        source="docs/width-conversion.md#width-conversion: narrow_to_wide merges bursts into target words"),
    "AXI.ROUTE.MODE": dict(
        text="A burst decodes in the address selectors of its first request and keeps that decision "
             "until it leaves the source; a mode-dependent window reaches its target only while "
             "its condition holds",
        source="docs/address-map.md#address-selectors; map definition targets when, address_selectors"),
    "AXI.ROUTE.EXPLICIT": dict(
        text="Every packet of a pair with an explicit route takes the links of that route, the "
             "request and the response route each",
        source="docs/network.md#routes; map definition.topology.routes, network request_routes"),
    "AXI.ROUTE.CONNECTIVITY": dict(
        text="A connected pair completes at its target; an unconnected pair answers at the "
             "source without any target handshake",
        source="docs/network.md#connectivity; map network.request_routes and admission"),
    "AXI.WIDTH.UPSIZE.MERGE": dict(
        text="Narrower source beats reach a wider AXI4 target merged into its words, each "
             "strobed byte once",
        source="docs/width-conversion.md#width-conversion, per-endpoint widths (narrow_to_wide)"),
    "AXI.WIDTH.ENDPOINT": dict(
        text="Every endpoint attaches at its own width: a burst reaches its target as the "
             "exact cut, merged or passed bursts of the width rules, each byte in its lane",
        source="docs/width-conversion.md#width-conversion; map contract.endpoint_widths"),
    "AXI.ATTR.MAP": dict(
        text="A target sees AxCACHE and AxPROT through its attribute map (zero, amba or "
             "explicit bit lists)",
        source="docs/user-signals.md#target-attributes"),
    "APB.INITIATOR.TRANSFER": dict(
        text="An APB initiator transfer is one AXI4-Lite beat: aligned, PSTRB as given, "
             "every failure PSLVERR with PRDATA zero",
        source="docs/protocols.md#apb-initiators"),
    "APB.TARGET.ACCESS": dict(
        text="An APB, AHB or AXI4-Lite target sees the beats of a burst as single transfers "
             "of its own word: strobes, lanes, attributes, errors as SLVERR",
        source="docs/dedicated-targets.md#dedicated-apb-and-axi4-lite-targets"),
    "APB.SUBPAGE.DECODE": dict(
        text="A window below 4 KiB owns its page; beats in the rest of the page answer DECERR "
             "without access",
        source="docs/address-map.md#target-mappings"),
    "AXI.ID.RETURN": dict(
        text="Every B and R carries the AxID of its request",
        source="AMBA AXI A5.2 transaction identifiers"),
    "AXI.LIVE.PREMISES": dict(
        text="Read and write packets share each network; an endpoint that breaks premise "
             "E1, E2 or E3 can stop it, and the bench names the broken premise",
        source="docs/timeout-and-progress.md#shared-request-and-response-paths"),
}

# Online rule IDs raised by the SV monitors (nlv_*.sv) and the tape
# interpreter (bench.py). The negative-control harness accepts a fault as
# CAUGHT only when the first failure rule or an offline mismatch kind is
# listed in that fault's expectation. LIVE.* rules are raised only when a
# tape wait times out (stall_report of the AXI BFMs).
RULES = (
    "AXI.AW.HOLD", "AXI.W.HOLD", "AXI.AR.HOLD", "AXI.B.HOLD", "AXI.R.HOLD",
    "AXI.RESET.QUIET", "AXI.X.AWVALID", "AXI.X.WVALID", "AXI.X.ARVALID", "AXI.X.BVALID",
    "AXI.X.RVALID", "AXI.W.LAST", "AXI.R.LAST", "AXI.B.UNEXPECTED", "AXI.R.UNEXPECTED",
    "AXI.B.BEFORE_WLAST",
    "APB.RESET.QUIET", "APB.X.PSEL", "APB.SETUP.ORDER", "APB.ACCESS.STABLE",
    "APB.ACCESS.ABANDON", "APB.PENABLE.DROP", "AHB.RESET.QUIET", "AHB.X.HTRANS",
    "AHB.X.CONTROL", "AHB.X.DATA", "AHB.X.RESPONSE", "AHB.DATA.HOLD",
    "AHB.CANCEL.ADDRESS", "AHB.RESPONSE.TWO_CYCLE",
    "AHB.ADDR.HOLD", "AHB.BUSY.OUTSIDE", "AHB.BUSY.CONTROL", "STREAM.HOLD", "CONTRACT.QUIET.AW",
    "CONTRACT.QUIET.W",
    "CONTRACT.QUIET.AR", "CONTRACT.QUIET.B", "CONTRACT.QUIET.R", "CONTRACT.QUIET.APB",
    "CONTRACT.QUIET.AHB", "CONTRACT.THROUGHPUT", "CONTRACT.TIMEOUT.GAP",
    "CONTRACT.OUTSTANDING.W", "CONTRACT.OUTSTANDING.R", "TAPE.EXPECT", "TAPE.WAIT_TIMEOUT",
    "TAPE.DRAIN_TIMEOUT", "TB.QUEUE_FULL", "TB.TAPE", "TB.BAD_BFM", "TB.BAD_OP",
    "LIVE.E1", "LIVE.E2", "LIVE.E3",
)

# Rules that only a four-state engine can raise; excluded from cross-engine
# count comparison and from two-state engines of the rule self-test.
FOUR_STATE_RULES = ("AXI.X.AWVALID", "AXI.X.WVALID", "AXI.X.ARVALID", "AXI.X.BVALID",
                    "AXI.X.RVALID", "APB.X.PSEL", "AHB.X.HTRANS", "AHB.X.CONTROL",
                    "AHB.X.DATA", "AHB.X.RESPONSE")

# Offline mismatch kinds raised by check.py.
MISMATCHES = (
    "SOURCE_UNANSWERED", "SOURCE_EXTRA_B", "SOURCE_EXTRA_R", "SOURCE_B_RESP", "SOURCE_R_RESP",
    "SOURCE_R_DATA", "SOURCE_R_USER", "SOURCE_R_COUNT", "SOURCE_R_LAST", "TARGET_UNATTRIBUTED",
    "TARGET_EXTRA_B", "TARGET_EXTRA_R", "TARGET_FOOTPRINT", "TARGET_WRITE_BYTES",
    "TARGET_DESCRIPTOR", "TARGET_ACCESS", "TARGET_PROT", "TARGET_ORDER", "ORDER_INTERPOSE",
    "STREAM_FIELD", "EVENT_X", "ACCOUNTING", "PROBE_GRANT", "REACH_MISS",
    "TARGET_UNSTROBED_DATA", "SOURCE_EARLY", "TARGET_ILLEGAL", "RATE_LOW",
    "ORACLE",
)

# Registered gaps: a rule or mismatch kind without negative-control evidence
# (registry_contracts gate). Each entry states why; an entry whose rule has
# evidence is stale and fails the gate. Empty: every rule and kind is proven
# by rulecheck.py (online), selftest.py (offline) or a designated fault.
GAPS = {}
