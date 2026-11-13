# Case registrations of the unified verification environment.
# Each RTL family has legal traffic and designated fault cases.

set(NLV_RUN ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/nlverif/run.py)
set(NLV_CASES ${CMAKE_CURRENT_SOURCE_DIR}/cases)
set(NLV_FIXTURE --fixture $<TARGET_FILE:nocloom_verif_fixture>)

nocloom_verif_case(NAME retirement_formal LAYER formal TIER formal TIMEOUT 3600
  TIMEOUT_REASON "Replay and cancellation models include completion covers and fault checks"
  FEATURES axi4 ahb MEMORY_MB 4096
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/retirement_formal.py ${NLV_FIXTURE})

nocloom_verif_case(NAME verif_selftest LAYER unit TIER quick TIMEOUT 60 FEATURES verif
  MEMORY_MB 256
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/selftest.py ${NLV_FIXTURE})
nocloom_verif_case(NAME verif_registry LAYER structure TIER quick TIMEOUT 30 FEATURES verif
  MEMORY_MB 256
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/registry.py
    --registry ${CMAKE_BINARY_DIR}/tests/verif-registry.json --source ${CMAKE_CURRENT_SOURCE_DIR})
# Every online monitor rule fires on a stub that breaks it, and stays silent
# on the stub's legal mode (registry_contracts evidence).
nocloom_verif_case(NAME verif_rulecheck LAYER unit TIER focused TIMEOUT 300 FEATURES verif
  MEMORY_MB 512
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/rulecheck.py
    --stub ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_rule_stub.v)

# AXI4-Lite target: the negative case runs the baseline first, then every
# designated fault; the same target with a report-only timer runs the
# exclusive writes after expiry with its own fault.
nocloom_verif_case(NAME verif_lite_target_32_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4_lite timeout exclusive MEMORY_MB 1024
  CONTRACTS AXI.ROUTE.LOCAL_OFFSET AXI.LITE.WORD_ACCESS AXI.TIMEOUT.EXACT AXI.EXCL.MONITOR
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/lite_target.py ${NLV_FIXTURE} --negative)

# Wide source -> fixed-width cutter -> 32-bit target, per source width. The
# 128-bit width also runs alone; every negative case runs the baseline first.
nocloom_verif_case(NAME verif_axi4_width_cut_128_32 LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 width MEMORY_MB 2048
  CONTRACTS AXI.WIDTH.DOWNSIZE.PIECES AXI.WIDTH.FIXED_REFUSE AXI.MERGE.SEVERITY AXI.RESET.CANCEL
    AXI.W.UNSTROBED_ZERO
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_width_cut.py ${NLV_FIXTURE})
foreach(bits IN ITEMS 64 128 256 512 1024)
  nocloom_verif_case(NAME verif_axi4_width_cut_${bits}_32_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 width MEMORY_MB 3072
    CONTRACTS AXI.WIDTH.DOWNSIZE.PIECES AXI.WIDTH.FIXED_REFUSE AXI.MERGE.SEVERITY
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_width_cut.py --param data_bits=${bits}
      ${NLV_FIXTURE} --negative)
endforeach()

nocloom_verif_case(NAME verif_qos_outstanding LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 qos arbiter MEMORY_MB 2048
  CONTRACTS AXI.QOS.LEVEL AXI.OUTSTANDING.REACH AXI.ROUTE.LOCAL_OFFSET AXI.MERGE.SEVERITY
    AXI.RESET.CANCEL AXI.ERR.READ_ZERO
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/qos_outstanding.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE})
nocloom_verif_case(NAME verif_qos_outstanding_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 qos arbiter MEMORY_MB 2048
  CONTRACTS AXI.QOS.LEVEL AXI.OUTSTANDING.REACH AXI.ROUTE.LOCAL_OFFSET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/qos_outstanding.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE} --negative)

# Liveness premises of shared read and write routes: the baseline keeps the
# endpoint premises; each scenario fault breaks one through the stimulus
# (same RTL) and must hang with the premise named (LIVE.E1..E3).
nocloom_verif_case(NAME verif_rw_merge_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 apb4 liveness MEMORY_MB 1024
  CONTRACTS AXI.LIVE.PREMISES APB.TARGET.ACCESS
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/rw_merge.py --cli $<TARGET_FILE:nocloom_cli> --negative)

# Configuration layer: the public CLI against data tables (accept, reject
# with the named code, artifact set, determinism across working directories).
set(NLV_CONFIG ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/config.py
  --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_config LAYER config TIER quick TIMEOUT 600 FEATURES config
  MEMORY_MB 512
  COMMAND ${NLV_CONFIG} --case ${NLV_CASES}/config_axi4.py --case ${NLV_CASES}/config_widths.py
    --case ${NLV_CASES}/config_arbitration.py
    --case ${NLV_CASES}/config_connectivity.py --case ${NLV_CASES}/config_mixed_widths.py
    --case ${NLV_CASES}/config_endpoint_widths.py
    --tool endpoint_data=$<TARGET_FILE:nocloom_verif_api_axi4_endpoint_data>
    --tool public=$<TARGET_FILE:nocloom_verif_api_axi4_public>
    --case ${NLV_CASES}/config_dedicated.py --case ${NLV_CASES}/config_outlets.py
    --case ${NLV_CASES}/config_shorthand.py --case ${NLV_CASES}/config_modes.py
    --case ${NLV_CASES}/config_routes.py
    --case ${NLV_CASES}/config_target_ids.py
    --tool timeout_public=$<TARGET_FILE:nocloom_verif_api_timeout_public>)
# Every shipped example: artifact set, determinism and the AHB HRESP width;
# the eight feature examples then run their directed protocol traffic.
nocloom_verif_case(NAME verif_examples LAYER structure TIER focused TIMEOUT 1200
  TIMEOUT_REASON "generates every example and simulates eight feature networks"
  PROCESSORS 2 FEATURES examples determinism MEMORY_MB 2048
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/examples.py
    --cli $<TARGET_FILE:nocloom_cli> ${NLV_FIXTURE}
    --config-case ${NLV_CASES}/config_examples.py --traffic-case ${NLV_CASES}/example_features.py)
# The two-domain SoC definition with every connected initiator-target pair.
nocloom_verif_case(NAME verif_soc_mixed LAYER rtl TIER focused TIMEOUT 300
  FEATURES examples axi4 ahb apb cdc width MEMORY_MB 2048
  CONTRACTS AXI.ROUTE.LOCAL_OFFSET AXI.WIDTH.DOWNSIZE.PIECES APB.TARGET.ACCESS AXI.CDC.CROSSING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/soc_mixed.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_soc_mixed_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES examples axi4 ahb apb cdc width MEMORY_MB 3072
  CONTRACTS AXI.WIDTH.DOWNSIZE.PIECES
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/soc_mixed.py --cli $<TARGET_FILE:nocloom_cli> --negative)
# The same examples from a CLI built with the other C++ compiler: no byte may
# differ. Without a second compiler the case reports skipped (77).
if(NOCLOOM_DETERMINISM_COMPILERS)
  nocloom_verif_case(NAME verif_determinism_compilers LAYER structure TIER release TIMEOUT 1800
    TIMEOUT_REASON "builds the CLI with a second compiler before generating every example"
    PROCESSORS 8 FEATURES examples determinism MEMORY_MB 4096
    COMMAND ${NLV_CONFIG} --case ${NLV_CASES}/config_examples.py
      --other-cxx=${nocloom_other_cxx} --source ${PROJECT_SOURCE_DIR} --cmake ${CMAKE_COMMAND}
      "--generator=${CMAKE_GENERATOR}" --jobs 8
      "--define=CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}" "--define=nlohmann_json_DIR=${nlohmann_json_DIR}"
      "--define=yaml-cpp_DIR=${yaml-cpp_DIR}" "--define=CLI11_DIR=${CLI11_DIR}")
  set_tests_properties(verif_determinism_compilers PROPERTIES SKIP_RETURN_CODE 77)
endif()

# Random-backpressure stress of the width converters and the packet splitter
# (public CLI), per mode: six fixed seeds (merge2b sixteen) and two more on
# the configuration with an exclusive-capable target.
foreach(mode IN ITEMS pw32 pw64 up128 cut128 cut1024 down64 down128 bounded32 bounded_pw
    bounded_up bounded_cut merge2 merge2b mix2 bsplit32 bsplit_pw bsplit_up bsplit_cut
    merge2auto merge2s
    mo_up128 mo_pw32 mo_merge2 mo_wide mo_bounded mo_bsplit mo_bsplit_up
    mo_down128 mo_cut1024 mo_bsplit_cut mo_merge2_id16 target_merge_id16)
  set(stress_contracts AXI.WIDTH.DOWNSIZE.PIECES AXI.WIDTH.FIXED_REFUSE AXI.MERGE.SEVERITY
    AXI.RESET.CANCEL AXI.W.UNSTROBED_ZERO AXI.ERR.READ_ZERO AXI.ERR.UNMAPPED_DECERR
    AXI.ROUTE.LOCAL_OFFSET)
  if(mode MATCHES "^mo_|_id16$")
    list(APPEND stress_contracts AXI.OUTSTANDING.REACH)
  endif()
  nocloom_verif_case(NAME verif_width_stress_${mode} LAYER rtl TIER focused TIMEOUT 600
    FEATURES axi4 width stress MEMORY_MB 2048 CONTRACTS ${stress_contracts}
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/width_stress.py --param mode=${mode}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
# Designated faults on the stress networks: a narrowing, a merging and a
# multi-burst mode (text faults on the generated port assignments).
foreach(mode IN ITEMS cut1024 merge2 mo_bsplit_up)
  nocloom_verif_case(NAME verif_width_stress_${mode}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 width stress MEMORY_MB 3072
    CONTRACTS AXI.W.UNSTROBED_ZERO AXI.ROUTE.LOCAL_OFFSET AXI.WIDTH.DOWNSIZE.PIECES
      AXI.MERGE.SEVERITY
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/width_stress.py --param mode=${mode}
      --cli $<TARGET_FILE:nocloom_cli> --negative)
endforeach()

# Beat rates through the public CLI: the width converters and the packet
# splitter (narrow-side within-burst gates, cut-burst spans, deep crossings)
# and a one-node graph whose read and write packets share each link.
foreach(mode IN ITEMS perwidth32 perwidth64 perwidth_up perwidth_cut perwidth_cut128 downsize64
    downsize128 downsize1024 bounded32 bounded_perwidth bounded_down64 bounded_cut cut_deep
    down_deep bounded_deep bounded_down_deep bounded_stall graph_single graph_multi graph_wide
    graph_deep)
  nocloom_verif_case(NAME verif_rate_${mode} LAYER rtl TIER focused TIMEOUT 600
    FEATURES axi4 width rate MEMORY_MB 2048
    CONTRACTS AXI.RATE.BEATS AXI.WIDTH.DOWNSIZE.PIECES AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/rates.py --param mode=${mode}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_rate_perwidth32_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 width rate MEMORY_MB 2048 CONTRACTS AXI.RATE.BEATS
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/rates.py --param mode=perwidth32
    --cli $<TARGET_FILE:nocloom_cli> --negative)

# Arbitration policies: eight networks under every policy and per-output
# overrides plus one with fixed QoS levels, each replayed cycle by cycle
# against the policy model (probe groups of the fixture recipe arbitration).
nocloom_verif_case(NAME verif_arbitration LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4 arbiter qos MEMORY_MB 2048 CONTRACTS AXI.ARB.POLICY AXI.QOS.LEVEL
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/arbitration.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE})
nocloom_verif_case(NAME verif_arbitration_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 arbiter MEMORY_MB 2048 CONTRACTS AXI.ARB.POLICY
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/arbitration.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE} --negative)

# Burst exchange compositions under sixteen directed phases: the two-source
# exchange at 32 and 64 bits (flat and laddered node words) and the
# three-source graph; each negative case runs its baselines first.
foreach(dut IN ITEMS exchange32 exchange64 graph_axi)
  nocloom_verif_case(NAME verif_exchange_${dut}_negative LAYER negative TIER focused TIMEOUT 600
    FEATURES axi4 exchange MEMORY_MB 2048 CONTRACTS AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/exchange_phases.py --param dut=${dut} ${NLV_FIXTURE}
      --negative)
endforeach()

# Install layer: the build installed once into a relocated prefix; consumers
# built with find_package only; installed CLI and Core give the same bytes;
# metadata controls; the examples' RTL through the verif stack on the selected
# simulator engine, with its negative control.
set(NLV_INSTALL ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/install.py
  --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR} --compiler ${CMAKE_CXX_COMPILER}
  --generator ${CMAKE_GENERATOR} --make-program ${CMAKE_MAKE_PROGRAM})
nocloom_verif_case(NAME verif_install_examples LAYER install TIER focused TIMEOUT 900
  TIMEOUT_REASON "installs once, builds four consumers and simulates ten example networks"
  PROCESSORS 2 FEATURES install examples MEMORY_MB 2048
  COMMAND ${NLV_INSTALL} --case ${NLV_CASES}/install_examples.py)
foreach(protocol IN ITEMS apb axi)
  nocloom_verif_case(NAME verif_install_scale_${protocol} LAYER install TIER release
    TIMEOUT 7200 TIMEOUT_REASON "six scale networks up to 256 sources and 256 targets or 64 clock domains, each compiled and simulated"
    PROCESSORS 4 FEATURES install examples large-network MEMORY_MB 8192
    COMMAND ${NLV_INSTALL} --case ${NLV_CASES}/install_scale.py --param protocol=${protocol})
endforeach()

# Clocked graphs: the three-source graph with selective pairs over switch
# nodes in their own domain (internal), with targets in a third (endpoints)
# or twelve domains, each with crossing FIFO and reset faults; the ratios
# case runs the endpoints network at six clock ratios and the twelve-domain
# one at random periods and phases.
foreach(mode IN ITEMS internal endpoints twelve)
  set(clocked_contracts AXI.ROUTE.LOCAL_OFFSET AXI.CDC.CROSSING)
  if(NOT mode STREQUAL "internal")
    list(APPEND clocked_contracts AXI.RESET.CANCEL)
  endif()
  nocloom_verif_case(NAME verif_graph_clocked_${mode}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 cdc graph MEMORY_MB 2048 CONTRACTS ${clocked_contracts}
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/graph_clocked.py --param mode=${mode} ${NLV_FIXTURE}
      --negative)
endforeach()
# The ratios case also holds the per-domain reset network (main configuration,
# public CLI): three reset ports released one domain at a time, with faults
# that tie a domain to another domain's reset port; every ratio configuration
# runs its baseline. Every network but the staggered one (clock_gating none) is gated; the
# gated staggered network (with faults that break a gate's enable), its copy with synchronous
# register reset, and one endpoints and the twelve network run their tape with the test port low
# and high: the same events. The gated staggered network, the twelve network and one
# endpoints ratio carry link pipes, the gated staggered network every endpoint pipe kind;
# faults drop the pipe slots from a gate's idle and clock pipes from another domain. Three more
# networks (oneside_inflight.py) reset one domain alone while a packet crosses its boundary;
# faults break a crossing side's reset handshake and the source's drop of unwaited responses.
nocloom_verif_case(NAME verif_graph_clocked_ratios LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 cdc graph MEMORY_MB 2048
  CONTRACTS AXI.RESET.DOMAIN AXI.CLOCK.GATE AXI.CDC.CROSSING AXI.PIPE.SLOTS
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/graph_clocked.py --param mode=ratios ${NLV_FIXTURE}
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# The aligned-burst AXI4 outlet behind an AXI4 front and a packet shim, per
# bus width: packet cases with the designated faults, the several-bursts
# outlet (32 and 1024 bits), beat rates and (32 bits) random stress.
foreach(bits IN ITEMS 32 64 128 256 512 1024)
  set(outlet_contracts AXI.OUTLET.FRAMING AXI.ROUTE.LOCAL_OFFSET AXI.MERGE.SEVERITY)
  nocloom_verif_case(NAME verif_axi4_outlet_${bits}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 outlet MEMORY_MB 3072 CONTRACTS ${outlet_contracts}
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_outlet.py --param data_bits=${bits}
      ${NLV_FIXTURE} --negative)
endforeach()
# The upsize merge behind an AXI4 front of the target width and an outlet,
# per target width: burst cases for sources 0 and 1 against its
# oracle and added faults; the context case (128 bits) fills fifteen (one
# direction at a time), two and seven merge contexts from two sources behind
# a front and an outlet each taking one burst more of a shared pool, with B
# reordered and a read tail held at the source.
foreach(bits IN ITEMS 32 128 256 512 1024)
  nocloom_verif_case(NAME verif_axi4_upsize_${bits}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 width upsize MEMORY_MB 3072 CONTRACTS AXI.UPSIZE.MERGE
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_upsize.py --param data_bits=${bits}
      ${NLV_FIXTURE} --negative)
endforeach()
nocloom_verif_case(NAME verif_axi4_upsize_context_negative LAYER negative TIER focused
  TIMEOUT 600 FEATURES axi4 width upsize MEMORY_MB 3072 CONTRACTS AXI.UPSIZE.MERGE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_upsize.py --param data_bits=128 --param mode=context
    ${NLV_FIXTURE} --negative)
# Per-target data widths through the CLI (cut, merge, fold and packet pieces,
# attribute maps), each mode with its designated text faults.
foreach(mode IN ITEMS graph clocked net64 packets attributes outstanding wide wide_packets
    wide_clocked)
  nocloom_verif_case(NAME verif_mixed_widths_${mode}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES axi4 width MEMORY_MB 4096 CONTRACTS AXI.WIDTH.ENDPOINT
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/mixed_widths.py --param mode=${mode}
      --cli $<TARGET_FILE:nocloom_cli> --negative)
endforeach()
# An AXI4 default target answers a write miss as the miss enters.
nocloom_verif_case(NAME verif_axi4_write_miss_answer LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 errors MEMORY_MB 1024
  CONTRACTS AXI.ERR.UNMAPPED_DECERR
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/axi4_write_miss_answer.py
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# Target IDs follow the route sum; requests of one ID wait, those of different IDs overtake.
nocloom_verif_case(NAME verif_target_id_rule LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.ID_RULE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_id_rule.py
    --cli $<TARGET_FILE:nocloom_cli> ${NLV_FIXTURE} --negative)
# A merged read's pool entry returns while the outlet's protocol zone rests.
nocloom_verif_case(NAME verif_merged_tail_gating LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.ROUTE.LOCAL_OFFSET AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/merged_tail_gating.py --cli $<TARGET_FILE:nocloom_cli>)
# A source's merger keeps its chosen request; responses free units as they enter the staging.
nocloom_verif_case(NAME verif_source_merge LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.SOURCE.MERGE AXI.ROUTE.LOCAL_OFFSET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/source_merge.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_source_merge_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.SOURCE.MERGE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/source_merge.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Two AXI4 targets interleave read data of different IDs beat by beat toward two sources.
nocloom_verif_case(NAME verif_target_interleave LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_interleave.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_target_interleave_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_interleave.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
nocloom_verif_case(NAME verif_target_read_granule LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_read_granule.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_target_read_granule_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_read_granule.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
nocloom_verif_case(NAME verif_read_decode_packet LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.ERR.READ_PACKET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/read_decode_packet.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_read_decode_packet_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.ERR.READ_PACKET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/read_decode_packet.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# One-burst AXI4 targets behind per-target merging take reads in response segments.
nocloom_verif_case(NAME verif_target_merged_segments LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_merged_segments.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_target_merged_segments_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_merged_segments.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# AXI4 targets with several bursts behind per-target merging alternate read data by target ID.
foreach(shape narrow single wide two third collide read_only wider)
  nocloom_verif_case(NAME verif_target_merged_read_interleave_${shape} LAYER rtl TIER focused
    TIMEOUT 300 FEATURES axi4 outstanding MEMORY_MB 2048
    CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_merged_read_interleave.py --param shape=${shape}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_target_merged_read_interleave_negative LAYER negative TIER focused
  TIMEOUT 600 FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_merged_read_interleave.py --param shape=narrow
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# Wider sources with reads of several IDs and a target alternating them word by word.
foreach(shape IN ITEMS wide64 wide128 two_sources wide_word)
  nocloom_verif_case(NAME verif_clamp_read_interleave_${shape} LAYER rtl TIER focused TIMEOUT 300
    FEATURES axi4 width outstanding MEMORY_MB 1024
    CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_read_interleave.py --param shape=${shape}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_clamp_read_interleave_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 width outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_read_interleave.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Two such sources and two such targets: each target alternates reads of both sources.
nocloom_verif_case(NAME verif_clamp_response_exchange LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4 width outstanding MEMORY_MB 1024
  CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_response_exchange.py
    --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_clamp_response_exchange_negative LAYER negative TIER focused
  TIMEOUT 600 FEATURES axi4 width outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_response_exchange.py
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# A read-only target that takes its ID from the plan interleaves by it toward a wider link.
nocloom_verif_case(NAME verif_read_only_interleave LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4 width outstanding MEMORY_MB 1024 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/read_only_interleave.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_read_only_interleave_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 width outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/read_only_interleave.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Sources with one read ID that read and write at once: crossed on two targets, or beside another.
foreach(shape IN ITEMS k4 k2 one_target)
  nocloom_verif_case(NAME verif_clamp_single_id_crossing_${shape} LAYER rtl TIER focused TIMEOUT 600
    FEATURES axi4 width outstanding MEMORY_MB 1024
    CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_single_id_crossing.py --param shape=${shape}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_clamp_single_id_crossing_negative LAYER negative TIER focused
  TIMEOUT 600 FEATURES axi4 width outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_single_id_crossing.py
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# Clamped sources with several bursts between one-burst targets, some of which do not interleave.
foreach(shape IN ITEMS single two wider)
  nocloom_verif_case(NAME verif_clamp_one_burst_targets_${shape} LAYER rtl TIER focused TIMEOUT 600
    FEATURES axi4 width outstanding MEMORY_MB 1024
    CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_one_burst_targets.py --param shape=${shape}
      --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_clamp_one_burst_targets_negative LAYER negative TIER focused
  TIMEOUT 600 FEATURES axi4 width outstanding MEMORY_MB 2048 CONTRACTS AXI.TARGET.R_INTERLEAVE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/clamp_one_burst_targets.py --param shape=two
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# Long WRAP bursts of wide sources and target lengths too short to carry them as one burst.
foreach(shape IN ITEMS 64 128 256)
  nocloom_verif_case(NAME verif_wrap_cut_${shape} LAYER rtl TIER focused TIMEOUT 300
    FEATURES axi4 width MEMORY_MB 1024 CONTRACTS AXI.ROUTE.LOCAL_OFFSET
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/wrap_cut.py --param source_bits=${shape}
      --param target_len=4 --cli $<TARGET_FILE:nocloom_cli>)
endforeach()
nocloom_verif_case(NAME verif_wrap_cut_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 width MEMORY_MB 2048 CONTRACTS AXI.ROUTE.LOCAL_OFFSET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/wrap_cut.py --param target_len=4
    --cli $<TARGET_FILE:nocloom_cli> --negative)
# Response beats of a narrow target gather into runs on a wider transport word.
nocloom_verif_case(NAME verif_response_runs LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 outstanding MEMORY_MB 2048
  CONTRACTS AXI.TARGET.R_INTERLEAVE AXI.ROUTE.LOCAL_OFFSET AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/response_runs.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_response_runs_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.OUTLET.FRAMING
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/response_runs.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Two sources of different widths merge into one wider target that stalls
# WREADY before every burst's last beat.
nocloom_verif_case(NAME verif_merge_contention LAYER rtl TIER focused TIMEOUT 300
  FEATURES axi4 width MEMORY_MB 1024
  CONTRACTS AXI.WIDTH.UPSIZE.MERGE AXI.ROUTE.LOCAL_OFFSET AXI.W.UNSTROBED_ZERO
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/merge_contention.py --cli $<TARGET_FILE:nocloom_cli>)
# Connectivity matrices (seven configurations), the stalled-target bound,
# the oracle controls and the band matrix over crossed links (a cycle that no
# route chains, configuration "crossed").
nocloom_verif_case(NAME verif_connectivity_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 connectivity MEMORY_MB 2048 CONTRACTS AXI.ROUTE.CONNECTIVITY
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/connectivity.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Mode-dependent mappings: four mode combinations from AXI4, AXI4-Lite and APB4 sources, a mode
# switch before a write's W and in the middle of a read and a write (baseline, then the faults).
nocloom_verif_case(NAME verif_mode_remap_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 modes MEMORY_MB 2048 CONTRACTS AXI.ROUTE.MODE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/mode_remap.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Explicit routes: every pair on its written request and response route, alone (each link of each
# transaction checked against the routes) and together, over a diamond, merged sources, merged
# targets, a route through one switch twice and two flows of one initiator.
nocloom_verif_case(NAME verif_explicit_routes_negative LAYER negative TIER focused TIMEOUT 1500
  TIMEOUT_REASON "five networks, each simulated with the two route swap faults"
  FEATURES axi4 apb4 routes MEMORY_MB 2048 CONTRACTS AXI.ROUTE.EXPLICIT
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/explicit_routes.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE} --negative)
# Unequal and wide source widths at native ports: contention and the cut
# cases on every route (data: four width sets; downsizeN: both sources N bits).
# Per-mode timeouts cover the full negative control (baseline plus the
# faulted build, which simulates the core and every fault variant
# concurrently): data runs four width sets, downsize256/512/1024 pay the
# wide-bus cycle cost in every faulted run.
foreach(mode IN ITEMS data downsize64 downsize128 downsize256 downsize512 downsize1024)
  if(mode STREQUAL "data")
    set(endpoint_contracts AXI.WIDTH.DOWNSIZE.PIECES)
  else()
    set(endpoint_contracts AXI.WIDTH.DOWNSIZE.PIECES AXI.ROUTE.LOCAL_OFFSET)
  endif()
  set(endpoint_timeout 600)
  set(endpoint_reason "")
  if(mode STREQUAL "data")
    set(endpoint_timeout 1500)
    set(endpoint_reason "four width-set configurations, each with its baseline and negative control")
  elseif(mode STREQUAL "downsize256")
    set(endpoint_timeout 1400)
    set(endpoint_reason "256-bit sources need a longer timeout")
  elseif(mode STREQUAL "downsize512")
    set(endpoint_timeout 2000)
    set(endpoint_reason "512-bit sources: the faulted build evaluates four DUT copies per cycle")
  elseif(mode STREQUAL "downsize1024")
    set(endpoint_timeout 3300)
    set(endpoint_reason "1024-bit sources: the faulted build evaluates four DUT copies per cycle")
  endif()
  nocloom_verif_case(NAME verif_endpoint_widths_${mode}_negative LAYER negative TIER focused
    TIMEOUT ${endpoint_timeout} TIMEOUT_REASON "${endpoint_reason}"
    FEATURES axi4 width MEMORY_MB 3072 CONTRACTS ${endpoint_contracts}
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/endpoint_widths.py --param mode=${mode}
      --cli $<TARGET_FILE:nocloom_cli> --negative)
endforeach()
# Dedicated AXI4-Lite, APB and AHB targets through the public CLI. The
# 64-bit Lite target runs every configuration's baseline, then the faults of
# its exclusive configuration (16-word exclusive admission, Lite source
# write storage).
nocloom_verif_case(NAME verif_lite_target64_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4_lite exclusive MEMORY_MB 1024
  CONTRACTS AXI.EXCL.MONITOR AXI.LITE.SOURCE_WRITE
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/lite_target64.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
nocloom_verif_case(NAME verif_lite_cut LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4_lite width MEMORY_MB 1024
  CONTRACTS AXI.LITE.WORD_ACCESS AXI.WIDTH.DOWNSIZE.PIECES AXI.OUTSTANDING.REACH
    AXI.W.UNSTROBED_ZERO
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/lite_cut.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_small_address LAYER rtl TIER focused TIMEOUT 600
  FEATURES apb axi4_lite MEMORY_MB 1024
  CONTRACTS AXI.ROUTE.LOCAL_OFFSET AXI.ERR.READ_ZERO AXI.RESET.CANCEL AXI.W.UNSTROBED_ZERO
    APB.INITIATOR.TRANSFER
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/small_address.py --cli $<TARGET_FILE:nocloom_cli>)
nocloom_verif_case(NAME verif_lite_unaligned LAYER rtl TIER focused TIMEOUT 600
  FEATURES axi4_lite apb ahb MEMORY_MB 1024
  CONTRACTS AXI.LITE.WORD_ACCESS AXI.ERR.UNMAPPED_DECERR AXI.ROUTE.LOCAL_OFFSET
    APB.TARGET.ACCESS APB.SUBPAGE.DECODE AXI.W.UNSTROBED_ZERO
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/lite_unaligned.py --cli $<TARGET_FILE:nocloom_cli>)
# The pipelined AXI4-Lite outlet: the negative run's baseline pins the
# back-to-back word rate at the target port and the 16-packets-in-flight
# phase; the faults hit the reload bypass and the AR channel timing.
nocloom_verif_case(NAME verif_lite_outlet_16_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4_lite MEMORY_MB 1024
  CONTRACTS AXI.RATE.BEATS AXI.ROUTE.LOCAL_OFFSET
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/lite_outlet.py --param outstanding=16 ${NLV_FIXTURE}
    --negative)
nocloom_verif_case(NAME verif_apb_target_widths LAYER rtl TIER focused TIMEOUT 600
  FEATURES apb MEMORY_MB 1024
  CONTRACTS APB.TARGET.ACCESS AXI.ERR.READ_ZERO AXI.RESET.CANCEL AXI.TIMEOUT.EXACT
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/apb_target_widths.py --cli $<TARGET_FILE:nocloom_cli>)
# Sub-page windows under a 40-bit address: one registration per group of
# modes (the first carries the designated faults, the rest are companion
# configurations of the same scenario).
foreach(group IN ITEMS graph uniform widths packets fixed)
  nocloom_verif_case(NAME verif_subpage_${group}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES apb ahb axi4_lite MEMORY_MB 2048
    CONTRACTS AXI.ROUTE.LOCAL_OFFSET APB.SUBPAGE.DECODE AXI.ID.RETURN APB.TARGET.ACCESS
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/subpage.py --param group=${group}
      --cli $<TARGET_FILE:nocloom_cli> --negative)
endforeach()
# Bounded packets at a multi-transaction AXI4 target through the public CLI:
# capacity 4 and 2 and 16-bit IDs (each its baseline), the pool, read order
# and cross-source B order judged from the target handshakes, and a source's
# pool of four transactions below a target of eight (a six-packet burst of
# src1 holds one entry); the faults run
# on capacity 4 (fixture recipe target_bounded, the CLI netlist byte for byte).
nocloom_verif_case(NAME verif_target_bounded_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES axi4 outstanding MEMORY_MB 2048 CONTRACTS AXI.OUTSTANDING.REACH AXI.ID.RETURN
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_bounded.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE} --negative)
# The APB4, APB3 and APB2 burst outlet behind an AXI4 front (fixture recipe
# apb_outlet): the case list per dialect and with FIXED single
# transfers; with back-to-back accesses (queue probes, a 2048-cycle
# timeout) alone and in a clocked network through the CLI (map storage
# release nets). Each configuration runs its baseline and its faults.
nocloom_verif_case(NAME verif_apb_outlet_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES apb outlet MEMORY_MB 2048
  CONTRACTS AXI.ROUTE.LOCAL_OFFSET APB.TARGET.ACCESS
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/apb_outlet.py --cli $<TARGET_FILE:nocloom_cli>
    ${NLV_FIXTURE} --negative)
# Target timeouts through the public CLI: twelve targets of every protocol,
# the hung-target episodes on one clock and with the targets on a clock
# three times slower; text faults stop four targets' timers (one clock). On
# one clock four more targets with network exclusive monitors run exclusive
# accesses after expiry, with oracle controls of those rules, and the source
# names its default target or leaves it to the smallest mapping.
nocloom_verif_case(NAME verif_target_timeouts_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES timeout apb ahb axi4_lite axi4 MEMORY_MB 3072 CONTRACTS AXI.TIMEOUT.EXACT
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/target_timeouts.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# The AHB outlet behind an AXI4 front of its width (fixture recipe
# ahb_outlet), per data width: the case list of every outlet variant and
# wait profile against the shape model and bus rules of cases/ahb_shape.py,
# each configuration with its baseline, the designated faults on the first.
foreach(bits IN ITEMS 32 64 128 256 512 1024)
  set(ahb_contracts APB.TARGET.ACCESS)
  if(bits EQUAL 32)
    list(APPEND ahb_contracts AXI.ROUTE.LOCAL_OFFSET AXI.LIVE.PREMISES)
  endif()
  nocloom_verif_case(NAME verif_ahb_outlet_${bits}_negative LAYER negative TIER focused
    TIMEOUT 600 FEATURES ahb outlet MEMORY_MB 3072
    CONTRACTS ${ahb_contracts}
    COMMAND ${NLV_RUN} --case ${NLV_CASES}/ahb_outlet.py --param data_bits=${bits} ${NLV_FIXTURE}
      --negative)
endforeach()
# Wide AHB targets in an explicit graph through the CLI (64 and 256 bits): a
# source of the target width, one twice as wide and a 32-bit one merged; the
# same networks with an idle 1024-bit target that makes every source cut its
# WRAP bursts.
nocloom_verif_case(NAME verif_ahb_wide_graph_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES ahb width MEMORY_MB 4096 CONTRACTS APB.TARGET.ACCESS AXI.WIDTH.ENDPOINT
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/ahb_wide_graph.py --cli $<TARGET_FILE:nocloom_cli>
    --negative)
# Packets overlapping at the several-packets AHB outlet behind an AXI4 front
# (fixture recipe ahb_outlet overlap=1), 32 and 1024 bits, plain, 2-bit
# HRESP, shaped and with a timeout; the faults run on 32 bits plain.
nocloom_verif_case(NAME verif_ahb_overlap_negative LAYER negative TIER focused TIMEOUT 600
  FEATURES ahb outlet MEMORY_MB 3072 CONTRACTS AXI.ID.RETURN APB.TARGET.ACCESS
  COMMAND ${NLV_RUN} --case ${NLV_CASES}/ahb_overlap.py ${NLV_FIXTURE} --negative)

# The service target on the network (not reachable from a configuration yet): the unmodified
# netlist against the bench, then each designated fault against the check that names it.
nocloom_verif_case(NAME verif_error_logger LAYER unit TIER focused TIMEOUT 600 FEATURES verif
  MEMORY_MB 1024
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/error_logger.py ${NLV_FIXTURE}
    --bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_logger_tb.sv
    --keep-bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_keep_logger_tb.sv
    --banks-bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_logger_banks_tb.sv
    --crossed-bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_crossed_logger_tb.sv)
nocloom_verif_case(NAME verif_service_target LAYER unit TIER focused TIMEOUT 600 FEATURES verif
  MEMORY_MB 1024
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/service_target.py ${NLV_FIXTURE}
    --bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_service_tb.sv)

# Register banks at their serial port (not reachable from a configuration yet): the unmodified
# netlist against the bench, then each designated fault against the check that names it.
nocloom_verif_case(NAME verif_register_bank LAYER unit TIER focused TIMEOUT 300 FEATURES verif
  MEMORY_MB 512
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/register_bank.py ${NLV_FIXTURE}
    --bench ${CMAKE_CURRENT_SOURCE_DIR}/sv/nlv_register_bank_tb.sv)


nocloom_verif_case(NAME mesh_topology_structure LAYER unit TIER quick TIMEOUT 120
  FEATURES topology MEMORY_MB 1024
  COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tests/run_mesh_topology.py
    --unit $<TARGET_FILE:nocloom_mesh_topology_unit>)
if(TARGET nocloom_mesh_example)
  nocloom_verif_case(NAME mesh_topology_example LAYER structure TIER quick TIMEOUT 120
    FEATURES topology MEMORY_MB 1024
    COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tests/run_mesh_topology.py
      --example $<TARGET_FILE:nocloom_mesh_example> --cli $<TARGET_FILE:nocloom_cli>)
  nocloom_verif_case(NAME mesh_topology_transactions LAYER rtl TIER focused TIMEOUT 600
    FEATURES topology axi4 MEMORY_MB 4096 PROCESSORS 4
    CONTRACTS AXI.ROUTE.LOCAL_OFFSET AXI.W.UNSTROBED_ZERO
    COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tests/run_mesh_topology.py
      --example $<TARGET_FILE:nocloom_mesh_example> --cli $<TARGET_FILE:nocloom_cli> --transactions
      --case ${NLV_CASES}/mesh_topology.py)
endif()
