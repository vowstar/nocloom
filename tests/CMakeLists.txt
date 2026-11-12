if(NOT NOCLOOM_BUILD_CONFIG OR NOT NOCLOOM_BUILD_CLI)
  message(FATAL_ERROR "Demo verification requires config and CLI targets")
endif()
find_package(Catch2 3.4.0...<4 CONFIG REQUIRED)
find_package(Threads REQUIRED)
find_package(Python3 COMPONENTS Interpreter REQUIRED)
add_executable(nocloom_unit unit/core.cpp unit/message.cpp)
target_sources(nocloom_unit PRIVATE unit/switch_graph.cpp)
target_sources(nocloom_unit PRIVATE unit/connectivity.cpp)
target_sources(nocloom_unit PRIVATE unit/canonical.cpp)
target_sources(nocloom_unit PRIVATE unit/address_table.cpp unit/modes.cpp unit/routes.cpp)
add_test(NAME canonical_ir COMMAND nocloom_unit "[canonical]")
set_tests_properties(canonical_ir PROPERTIES TIMEOUT 60 LABELS "unit;quick")
add_test(NAME connectivity_structure COMMAND nocloom_unit "[connectivity]")
set_tests_properties(connectivity_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")
add_test(NAME dependency_release_cone COMMAND nocloom_unit "[.dependency-cone]")
set_tests_properties(dependency_release_cone PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 900 LABELS "unit;focused")
add_test(NAME dependency_declarations
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_dependency_declarations.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/dependency_declarations)
set_tests_properties(dependency_declarations PROPERTIES TIMEOUT 300 LABELS "formal;focused")
# Bounded liveness of small networks (run_liveness_formal.py), with the controls of each assumption.
foreach(part IN ITEMS "prove_a;^L[123]_prove$" "prove_b;^L[45678].*_prove$" "hangs;_hang" "needed;_needed$" "covers;cover")
  list(GET part 0 part_name)
  list(GET part 1 part_regex)
  add_test(NAME liveness_formal_${part_name}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_liveness_formal.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/liveness_formal_${part_name}
      --only "${part_regex}")
  set_tests_properties(liveness_formal_${part_name} PROPERTIES TIMEOUT 3600 LABELS "formal;liveness")
endforeach()
add_test(NAME liveness_formal_driver
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_liveness_formal.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/liveness_formal_driver --self-test)
set_tests_properties(liveness_formal_driver PROPERTIES TIMEOUT 120 LABELS "formal;quick")
add_test(NAME connectivity_formal
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_connectivity_formal.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/connectivity_formal)
set_tests_properties(connectivity_formal PROPERTIES TIMEOUT 3600 LABELS "formal;focused")
# Model and certificate proofs. Lean is required: a missing tool fails the test.
add_test(NAME formal_lean
  COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/formal/lean/tools/run_checks.py
    --work ${CMAKE_CURRENT_BINARY_DIR}/formal_lean)
set_tests_properties(formal_lean PROPERTIES TIMEOUT 900 LABELS "formal;release")
target_sources(nocloom_unit PRIVATE unit/width_adapter.cpp)
target_sources(nocloom_unit PRIVATE unit/axi4_config.cpp unit/axi3.cpp)
# The shorthand pairs read tests/shorthand and examples from the source tree.
target_sources(nocloom_unit PRIVATE unit/shorthand.cpp ${PROJECT_SOURCE_DIR}/cli/diff.cpp)
target_include_directories(nocloom_unit PRIVATE ${PROJECT_SOURCE_DIR}/cli)
target_compile_definitions(nocloom_unit PRIVATE NOCLOOM_SOURCE_DIR="${PROJECT_SOURCE_DIR}")
target_sources(nocloom_unit PRIVATE unit/multi_outstanding.cpp)
target_sources(nocloom_unit PRIVATE unit/exclusive.cpp)
add_test(NAME exclusive_structure COMMAND nocloom_unit "[exclusive]")
set_tests_properties(exclusive_structure PROPERTIES
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit TIMEOUT 120 LABELS "unit;axi4")
add_test(NAME multi_outstanding_structure COMMAND nocloom_unit "[multi-outstanding]")
set_tests_properties(multi_outstanding_structure PROPERTIES
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit TIMEOUT 120 LABELS "unit;axi4")
target_sources(nocloom_unit PRIVATE unit/lite_outstanding.cpp)
add_test(NAME lite_outstanding_structure COMMAND nocloom_unit "[lite-outstanding]")
set_tests_properties(lite_outstanding_structure PROPERTIES
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit TIMEOUT 120 LABELS "unit;axi4")
add_test(NAME axi4_config COMMAND nocloom_unit "[axi4-config]")
set_tests_properties(axi4_config PROPERTIES TIMEOUT 180 LABELS "unit;config")
target_sources(nocloom_unit PRIVATE unit/axi4_initiator.cpp)
add_test(NAME axi4_initiator_structure COMMAND nocloom_unit "[axi4-initiator]")
set_tests_properties(axi4_initiator_structure PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 60 LABELS "unit;axi4-initiator")

add_executable(nocloom_axi4_initiator_fixture generate_axi4_initiator.cpp)
target_link_libraries(nocloom_axi4_initiator_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_axi4_initiator_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
foreach(config IN ITEMS "32;8;4" "64;4;0" "128;8;4")
  list(GET config 0 ai_data)
  list(GET config 1 ai_len)
  list(GET config 2 ai_id)
  add_test(NAME axi4_initiator_${ai_data}b_len${ai_len}_id${ai_id}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_initiator.py
      --fixture $<TARGET_FILE:nocloom_axi4_initiator_fixture>
      --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_initiator_${ai_data}b_len${ai_len}_id${ai_id}
      --data ${ai_data} --len-bits ${ai_len} --id-bits ${ai_id})
  set_tests_properties(axi4_initiator_${ai_data}b_len${ai_len}_id${ai_id} PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR} TIMEOUT 900 LABELS "rtl;axi4-initiator")
endforeach()
target_link_libraries(nocloom_unit PRIVATE nocloom_io Catch2::Catch2WithMain Threads::Threads nlohmann_json::nlohmann_json)
target_include_directories(nocloom_unit PRIVATE ${PROJECT_SOURCE_DIR}/src)
file(MAKE_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit)
add_test(NAME unit COMMAND nocloom_unit)
# The unit binary is compute bound: an unoptimized build (Debug, or an empty
# build type in a parent project) runs it several times slower, so it gets a
# wider bound there. The top level defaults to Release. It is in the quick set:
# every unit case runs in seconds and catches contract changes before a full run.
# The bounds leave room for a shared two-core runner under parallel load.
if(CMAKE_BUILD_TYPE MATCHES "^(Debug|)$")
  set(NOCLOOM_UNIT_TIMEOUT 1800)
else()
  set(NOCLOOM_UNIT_TIMEOUT 600)
endif()
set_tests_properties(unit PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT ${NOCLOOM_UNIT_TIMEOUT} LABELS "unit;quick")
add_test(NAME switch_graph COMMAND nocloom_unit "[switch-graph]")
set_tests_properties(switch_graph PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 30 LABELS "unit;switch-graph")
add_test(NAME width_adapter COMMAND nocloom_unit "[width-adapter]")
set_tests_properties(width_adapter PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 30 LABELS "unit;width-adapter")
add_executable(nocloom_width_adapter_fixture generate_width_adapter.cpp)
target_link_libraries(nocloom_width_adapter_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_width_adapter_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
foreach(hop IN ITEMS "32;16" "16;8" "8;4")
  list(GET hop 0 wa_wide)
  list(GET hop 1 wa_narrow)
  add_test(NAME width_adapter_roundtrip_${wa_wide}x${wa_narrow}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_width_adapter.py
    --fixture $<TARGET_FILE:nocloom_width_adapter_fixture> --wide ${wa_wide} --narrow ${wa_narrow}
    --work ${CMAKE_CURRENT_BINARY_DIR}/width_adapter_roundtrip_${wa_wide}x${wa_narrow})
  set_tests_properties(width_adapter_roundtrip_${wa_wide}x${wa_narrow} PROPERTIES
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR} TIMEOUT 300 LABELS "rtl;width-adapter")
endforeach()
add_test(NAME message_layout COMMAND nocloom_unit "[message]")
set_tests_properties(message_layout PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 30 LABELS "unit;message")
foreach(tool IN ITEMS iverilog yosys verilator)
  find_program(NOCLOOM_TOOL_${tool} ${tool})
  if(NOT NOCLOOM_TOOL_${tool} AND NOCLOOM_TEST_DEPS_REQUIRED)
    message(FATAL_ERROR "Required verification tool missing: ${tool}")
  endif()
endforeach()
foreach(case IN ITEMS cli install)
  set(case_toolchain)
  if(case STREQUAL "install")
    list(APPEND case_toolchain --generator "${CMAKE_GENERATOR}")
    if(CMAKE_CXX_COMPILER)
      list(APPEND case_toolchain --compiler "${CMAKE_CXX_COMPILER}")
    endif()
    if(CMAKE_MAKE_PROGRAM)
      list(APPEND case_toolchain --make-program "${CMAKE_MAKE_PROGRAM}")
    endif()
  endif()
  add_test(NAME ${case} COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run.py
    --case ${case} --cli $<TARGET_FILE:nocloom_cli> --source ${PROJECT_SOURCE_DIR}
    --build ${PROJECT_BINARY_DIR} ${case_toolchain}
    --work ${CMAKE_CURRENT_BINARY_DIR}/${case})
  set_tests_properties(${case} PROPERTIES TIMEOUT 600 LABELS integration
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
set_tests_properties(install PROPERTIES LABELS install PROCESSORS 2)
target_sources(nocloom_unit PRIVATE unit/ipxact.cpp)
add_test(NAME ipxact_structure COMMAND nocloom_unit "[ipxact]")
set_tests_properties(ipxact_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")
# The IEEE 1685-2009 schema is not part of the source tree. Point NOCLOOM_IPXACT_SCHEMA_DIR at an
# unmodified copy of the published files (index.xsd and the files it includes) to register the
# schema validation of every example's component.
set(NOCLOOM_IPXACT_SCHEMA_DIR "" CACHE PATH "IEEE 1685-2009 schema directory (index.xsd)")
if(NOCLOOM_IPXACT_SCHEMA_DIR STREQUAL "")
  message(STATUS "NOT RUN: ipxact_schema (set NOCLOOM_IPXACT_SCHEMA_DIR to register it)")
else()
  find_program(NOCLOOM_TOOL_xmllint xmllint)
  if(NOT NOCLOOM_TOOL_xmllint OR NOT EXISTS ${NOCLOOM_IPXACT_SCHEMA_DIR}/index.xsd)
    message(FATAL_ERROR "ipxact_schema requires xmllint and ${NOCLOOM_IPXACT_SCHEMA_DIR}/index.xsd")
  endif()
  add_test(NAME ipxact_schema COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run.py
    --case ipxact --cli $<TARGET_FILE:nocloom_cli> --source ${PROJECT_SOURCE_DIR}
    --build ${PROJECT_BINARY_DIR} --schema ${NOCLOOM_IPXACT_SCHEMA_DIR}
    --xmllint ${NOCLOOM_TOOL_xmllint} --work ${CMAKE_CURRENT_BINARY_DIR}/ipxact_schema)
  set_tests_properties(ipxact_schema PROPERTIES TIMEOUT 900 LABELS integration
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endif()
# Library process boundary (no I/O, console output or exit), installed header
# surface, IR verifier and failed-generation emptiness, each with a control.
add_executable(nocloom_library_boundary_fixture library_boundary.cpp)
target_link_libraries(nocloom_library_boundary_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_library_boundary_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME library_boundary_facilities
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_verification_facilities.py
    --boundary $<TARGET_FILE:nocloom_library_boundary_fixture>
    --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR} --compiler ${CMAKE_CXX_COMPILER}
    "--include-dir=$<JOIN:$<TARGET_PROPERTY:nocloom_library_boundary_fixture,INCLUDE_DIRECTORIES>,;--include-dir=>"
    "--include-dir=$<JOIN:$<TARGET_PROPERTY:nocloom_core,INCLUDE_DIRECTORIES>,;--include-dir=>"
    "--include-dir=$<JOIN:$<TARGET_PROPERTY:nocloom_cli,INCLUDE_DIRECTORIES>,;--include-dir=>"
    --include-dir=${PROJECT_SOURCE_DIR}/src/core
    --core-library $<TARGET_FILE:nocloom_core>
    --io-library $<TARGET_FILE:nocloom_io>
    --config-library $<TARGET_FILE:nocloom_config>
    --yaml-library $<TARGET_FILE:yaml-cpp::yaml-cpp>
    --work ${CMAKE_CURRENT_BINARY_DIR}/library_boundary_facilities
  COMMAND_EXPAND_LISTS)
set_tests_properties(library_boundary_facilities PROPERTIES TIMEOUT 240 LABELS "facility;mutation")
if(TARGET nocloom_api_example)
  add_test(NAME api_example COMMAND nocloom_api_example)
  set_tests_properties(api_example PROPERTIES TIMEOUT 20 LABELS integration
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endif()

add_executable(nocloom_message_contract_fixture message_contract.cpp)
target_link_libraries(nocloom_message_contract_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_message_contract_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME message_contract_mutations
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_message_contract.py
    --source ${PROJECT_SOURCE_DIR} --compiler ${CMAKE_CXX_COMPILER}
    --fixture $<TARGET_FILE:nocloom_message_contract_fixture>
    "--include-dir=$<JOIN:$<TARGET_PROPERTY:nocloom_message_contract_fixture,INCLUDE_DIRECTORIES>,;--include-dir=>"
    --include-dir=${PROJECT_SOURCE_DIR}/src/core
    --work ${CMAKE_CURRENT_BINARY_DIR}/message_contract_mutations
  COMMAND_EXPAND_LISTS)
set_tests_properties(message_contract_mutations PROPERTIES TIMEOUT 180 LABELS "message;mutation;unit")







# Gray-count asynchronous FIFO crossing of clocked graphs.
target_sources(nocloom_unit PRIVATE unit/async_fifo.cpp)
add_test(NAME async_fifo_structure COMMAND nocloom_unit "[async_fifo]")
set_tests_properties(async_fifo_structure PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 120 LABELS "unit;cdc")

add_executable(nocloom_async_fifo_fixture generate_async_fifo.cpp generate_fifo_levels.cpp)
target_link_libraries(nocloom_async_fifo_fixture PRIVATE nocloom_io nocloom_core nlohmann_json::nlohmann_json)
target_include_directories(nocloom_async_fifo_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
foreach(case IN ITEMS ratios throughput faults)
  add_test(NAME async_fifo_${case}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_async_fifo.py --case ${case}
      --fixture $<TARGET_FILE:nocloom_async_fifo_fixture>
      --work ${CMAKE_CURRENT_BINARY_DIR}/async_fifo_${case})
  set_tests_properties(async_fifo_${case} PROPERTIES TIMEOUT 1800 LABELS "rtl;cdc;async-fifo"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Unbounded control proofs; ORDER and SLOT_STABLE unbounded on most shapes, bounded on
# the largest (see run_async_fifo_formal.py),
# covers and fault BMC; a missing sby fails the test.
add_test(NAME async_fifo_formal
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_async_fifo_formal.py
    --check-driver
    --fixture $<TARGET_FILE:nocloom_async_fifo_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/async_fifo_formal)
set_tests_properties(async_fifo_formal PROPERTIES TIMEOUT 5400 PROCESSORS 8
  LABELS "formal;cdc;async-fifo" WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})

target_sources(nocloom_unit PRIVATE unit/related_clock.cpp unit/related_clock_public.cpp)
add_test(NAME related_clock_structure COMMAND nocloom_unit "[related-clock]")
set_tests_properties(related_clock_structure PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 120 LABELS "unit;quick;cdc")
add_test(NAME related_clock_public_structure COMMAND nocloom_unit "[related-clock-public]")
set_tests_properties(related_clock_public_structure PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 180 LABELS "unit;quick;cdc")
add_executable(nocloom_related_clock_fixture generate_related_clock.cpp)
target_include_directories(nocloom_related_clock_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_related_clock_fixture PRIVATE nocloom_core nlohmann_json::nlohmann_json)
add_test(NAME related_clock_transactions
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_related_clock.py
    --fixture $<TARGET_FILE:nocloom_related_clock_fixture>
    --nocloom $<TARGET_FILE:nocloom_cli>
    --work ${CMAKE_CURRENT_BINARY_DIR}/related_clock_transactions)
set_tests_properties(related_clock_transactions PROPERTIES TIMEOUT 900 PROCESSORS 4
  LABELS "rtl;focused;cdc" WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})

# The adapter contract and the process contract run as two entries. Each
# keeps its scratch directories in the build tree.
add_test(NAME simulation_engine_contract COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/test_sim_engine.py Contract)
add_test(NAME simulation_process_contract COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/test_sim_engine.py ProcessContract)
set_tests_properties(simulation_engine_contract simulation_process_contract PROPERTIES TIMEOUT 90
  LABELS "unit;simulation" WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})


add_test(NAME simulation_metrics_contract COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/test_sim_metrics.py)
set_tests_properties(simulation_metrics_contract PROPERTIES TIMEOUT 30
  LABELS "unit;simulation" WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})

foreach(contract IN ITEMS runner history adaptive probe)
  add_test(NAME suite_${contract}_contract
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_suite_${contract}.py -v)
  set_tests_properties(suite_${contract}_contract PROPERTIES TIMEOUT 60 LABELS "unit;verification"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()

# ---- burst composition: AXI4 burst exchange composition (internal profile) ----
target_sources(nocloom_unit PRIVATE unit/axi4_exchange.cpp)
add_test(NAME axi4_exchange_structure COMMAND nocloom_unit "[axi4-exchange]")
set_tests_properties(axi4_exchange_structure PROPERTIES WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/unit
  TIMEOUT 60 LABELS "unit;axi4-exchange")


# Local hold-discipline proofs of the width adapter pair.
add_executable(nocloom_formal2_fixture generate_formal2_adapter.cpp)
target_link_libraries(nocloom_formal2_fixture PRIVATE nocloom_core nlohmann_json::nlohmann_json)
target_include_directories(nocloom_formal2_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME formal2_prepare COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/run_formal2.py --fixture $<TARGET_FILE:nocloom_formal2_fixture>
  --work ${CMAKE_CURRENT_BINARY_DIR}/formal2_prepared --case prepare)
set_tests_properties(formal2_prepare PROPERTIES TIMEOUT 120 LABELS "formal;formal-inputs"
  FIXTURES_SETUP formal2_artifacts)
add_test(NAME formal2_adapter_axioms COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/run_formal2.py --prepared ${CMAKE_CURRENT_BINARY_DIR}/formal2_prepared
  --work ${CMAKE_CURRENT_BINARY_DIR}/formal2_adapter_axioms --case adapter_axioms)
set_tests_properties(formal2_adapter_axioms PROPERTIES TIMEOUT 900 LABELS "formal;local-safety"
  FIXTURES_REQUIRED formal2_artifacts)

# Internal aligned-burst master outlet checks.
target_sources(nocloom_unit PRIVATE unit/axi4_outlet.cpp)
target_sources(nocloom_unit PRIVATE unit/axi4_width_cut.cpp)
add_test(NAME axi4_width_cut_structure COMMAND nocloom_unit "[axi4-width-cut]")
set_tests_properties(axi4_width_cut_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")
target_sources(nocloom_unit PRIVATE unit/converter_stages.cpp)
add_test(NAME converter_stages_structure COMMAND nocloom_unit "[converter-stages]")
set_tests_properties(converter_stages_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")
# The fixture cases of every source width run as verif_axi4_width_cut_* (tests/verif).
add_test(NAME axi4_outlet_structure COMMAND nocloom_unit "[axi4-outlet]")
set_tests_properties(axi4_outlet_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")
# The outlet and the upsize merge run as verif_axi4_outlet_* and
# verif_axi4_upsize_* (tests/verif).

add_test(NAME axi4_public_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_public.py
    --build ${PROJECT_BINARY_DIR} --source ${PROJECT_SOURCE_DIR}
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_public_installed
    --compiler ${CMAKE_CXX_COMPILER})
set_tests_properties(axi4_public_installed PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;installed;focused")

add_test(NAME axi4_address48_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_public.py
    --build ${PROJECT_BINARY_DIR} --source ${PROJECT_SOURCE_DIR}
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_address48_installed
    --compiler ${CMAKE_CXX_COMPILER} --address48)
set_tests_properties(axi4_address48_installed PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;installed;focused")

add_test(NAME axi4_endpoint_addresses_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_public.py
    --build ${PROJECT_BINARY_DIR} --source ${PROJECT_SOURCE_DIR}
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_endpoint_addresses_installed
    --compiler ${CMAKE_CXX_COMPILER} --heterogeneous)
set_tests_properties(axi4_endpoint_addresses_installed PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;installed;focused")

add_test(NAME axi4_mixed_ingress_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_public.py
    --build ${PROJECT_BINARY_DIR} --source ${PROJECT_SOURCE_DIR}
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_mixed_ingress_installed
    --compiler ${CMAKE_CXX_COMPILER} --mixed)
set_tests_properties(axi4_mixed_ingress_installed PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;installed;focused")





# Dedicated target-side AHB transfer sequencing.
target_sources(nocloom_unit PRIVATE unit/ahb_outlet.cpp)
add_executable(nocloom_axi4_packetizer_fixture generate_axi4_packetizer.cpp)
target_link_libraries(nocloom_axi4_packetizer_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_axi4_packetizer_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
# The splitter alone: packet order and B absorption under random ready.
add_test(NAME axi4_packetizer_order
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_packetizer.py
    --fixture $<TARGET_FILE:nocloom_axi4_packetizer_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_packetizer_order)
set_tests_properties(axi4_packetizer_order PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;focused")
add_test(NAME axi4_packetizer_multi
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_packetizer_multi.py
    --fixture $<TARGET_FILE:nocloom_axi4_packetizer_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_packetizer_multi)
set_tests_properties(axi4_packetizer_multi PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;focused"
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})

add_test(NAME ahb_outlet_structure COMMAND nocloom_unit "[ahb-outlet]")
set_tests_properties(ahb_outlet_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")

target_sources(nocloom_unit PRIVATE unit/ahb_public.cpp)
add_test(NAME ahb_public_structure COMMAND nocloom_unit "[ahb-public]")
set_tests_properties(ahb_public_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")
add_test(NAME ahb_public_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_ahb_public.py
    --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
    --compiler ${CMAKE_CXX_COMPILER} --work ${CMAKE_CURRENT_BINARY_DIR}/ahb_public_installed)
set_tests_properties(ahb_public_installed PROPERTIES TIMEOUT 900 LABELS "rtl;installed;focused")
add_test(NAME ahb_target_protocol
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_ahb_public.py
    --build ${PROJECT_BINARY_DIR} --source ${PROJECT_SOURCE_DIR}
    --compiler ${CMAKE_CXX_COMPILER} --cli $<TARGET_FILE:nocloom_cli>
    --work ${CMAKE_CURRENT_BINARY_DIR}/ahb_target_protocol --protocol-only)
set_tests_properties(ahb_target_protocol PROPERTIES TIMEOUT 900 LABELS "rtl;ahb;focused")

target_sources(nocloom_unit PRIVATE unit/graph_axi.cpp)
add_test(NAME graph_axi_structure COMMAND nocloom_unit "[graph-axi]")
set_tests_properties(graph_axi_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")

target_sources(nocloom_unit PRIVATE unit/graph_example.cpp)
add_test(NAME graph_example_structure COMMAND nocloom_unit "[graph-example]")
set_tests_properties(graph_example_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")
add_test(NAME graph_example_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_graph_example.py
    --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
    --compiler ${CMAKE_CXX_COMPILER} --work ${CMAKE_CURRENT_BINARY_DIR}/graph_example_installed)
set_tests_properties(graph_example_installed PROPERTIES TIMEOUT 900 LABELS "rtl;installed;focused")

add_test(NAME graph_selective_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_graph_example.py
    --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
    --compiler ${CMAKE_CXX_COMPILER} --work ${CMAKE_CURRENT_BINARY_DIR}/graph_selective_installed
    --selective)
set_tests_properties(graph_selective_installed PROPERTIES TIMEOUT 900 LABELS "rtl;installed;focused")
target_sources(nocloom_unit PRIVATE unit/selective_graph.cpp)

add_test(NAME graph_cross_installed
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_graph_example.py
    --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
    --compiler ${CMAKE_CXX_COMPILER} --work ${CMAKE_CURRENT_BINARY_DIR}/graph_cross_installed
    --cross)
set_tests_properties(graph_cross_installed PROPERTIES TIMEOUT 900 LABELS "rtl;installed;focused")


# Clock placement derives real graph segments before CDC lowering.
target_sources(nocloom_unit PRIVATE unit/exchange_clocking.cpp)
add_test(NAME exchange_clocking COMMAND nocloom_unit "[exchange-clocking]")
set_tests_properties(exchange_clocking PROPERTIES TIMEOUT 180 LABELS "unit;quick;cdc")

# The clocked graph networks (internal, endpoints, twelve domains, clock
# ratios) run as verif_graph_clocked_* (tests/verif).
# Beat rates through one crossing per direction against the same network in one domain.
add_test(NAME graph_clocked_throughput
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_graph_clocked_throughput.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/graph_clocked_throughput)
set_tests_properties(graph_clocked_throughput PROPERTIES TIMEOUT 1800 LABELS "rtl;cdc;graph")

target_sources(nocloom_unit PRIVATE unit/graph_clocked_example.cpp)
add_test(NAME graph_clocked_example_structure COMMAND nocloom_unit "[graph-clocked-example]")
set_tests_properties(graph_clocked_example_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick;cdc")
foreach(mode IN ITEMS endpoints twelve)
  add_test(NAME graph_clocked_installed_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_graph_clocked_example.py
      --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
      --compiler ${CMAKE_CXX_COMPILER} --work ${CMAKE_CURRENT_BINARY_DIR}/graph_clocked_installed_${mode}
      --mode ${mode})
  set_tests_properties(graph_clocked_installed_${mode} PROPERTIES TIMEOUT 600 LABELS "rtl;installed;cdc;focused")
endforeach()

target_sources(nocloom_unit PRIVATE unit/apb_burst_outlet.cpp)
add_test(NAME apb_burst_outlet_structure COMMAND nocloom_unit "[apb-burst-outlet]")
set_tests_properties(apb_burst_outlet_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")

target_sources(nocloom_unit PRIVATE unit/apb_burst_example.cpp)
add_test(NAME apb_burst_example_structure COMMAND nocloom_unit "[apb-burst-example]")
set_tests_properties(apb_burst_example_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")
foreach(mode IN ITEMS funnel graph clocked)
  add_test(NAME apb_burst_installed_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_apb_public.py
      --source ${PROJECT_SOURCE_DIR} --build ${PROJECT_BINARY_DIR}
      --compiler ${CMAKE_CXX_COMPILER}
      --work ${CMAKE_CURRENT_BINARY_DIR}/apb_burst_installed_${mode}
      --mode ${mode})
  set_tests_properties(apb_burst_installed_${mode} PROPERTIES TIMEOUT 900 LABELS "rtl;installed;focused")
endforeach()
add_executable(nocloom_timeout_outlet_fixture generate_timeout_outlet.cpp)
target_link_libraries(nocloom_timeout_outlet_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_timeout_outlet_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
foreach(kind IN ITEMS apb4 apb3 axi4_lite ahb_v3 axi4)
  # The AXI4-Lite outlet safety checks run bounded to 12 cycles. Expiry
  # reachability needs a 1030-frame trace and is covered by
  # verif_target_timeouts_negative. The other outlets are proved unbounded.
  # The AXI4 outlet is also proved with two bursts in flight (its expiry is
  # reached in verif_target_timeouts_negative).
  if(kind STREQUAL "axi4_lite")
    set(formal_mode_args --mode bmc --depth 12 --tasks check)
  elseif(kind STREQUAL "axi4")
    set(formal_mode_args --capacities 1,2)
  else()
    set(formal_mode_args)
  endif()
  add_test(NAME target_timeout_formal_${kind}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_timeout_formal.py
      --fixture $<TARGET_FILE:nocloom_timeout_outlet_fixture>
      --work ${CMAKE_CURRENT_BINARY_DIR}/target_timeout_formal_${kind} --kind ${kind}
      ${formal_mode_args})
  set_tests_properties(target_timeout_formal_${kind} PROPERTIES TIMEOUT 5400 LABELS "formal;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Schema 10 APB4 and APB3 initiators mixed with AXI sources and every target protocol.
foreach(apb_mode IN ITEMS graph tree clocked funnel apb4_tree timeout selective multi narrow narrow_clocked)
  add_test(NAME apb_initiator_${apb_mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_apb_initiator.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/apb_initiator_${apb_mode}
      --mode ${apb_mode})
  set_tests_properties(apb_initiator_${apb_mode} PROPERTIES TIMEOUT 1200 LABELS "rtl;axi4;apb;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Schema 10 AHB-Lite initiators: every HBURST, BUSY, ERROR, early ends and width conversion.
foreach(ahb_mode IN ITEMS switch wide clocked)
  add_test(NAME ahb_initiator_${ahb_mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_ahb_initiator.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/ahb_initiator_${ahb_mode}
      --mode ${ahb_mode})
  set_tests_properties(ahb_initiator_${ahb_mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;ahb;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Schema 10 AXI3 initiators: two-bit AxLOCK, WID, width conversion and exclusive pairs; AXI3
# targets (axi3_target): WID in AW order with two bursts in flight, AxLOCK 0b01 in target mode.
foreach(axi3_case IN ITEMS initiator_switch:switch initiator_wide:wide target:target)
  string(REPLACE ":" ";" axi3_parts ${axi3_case})
  list(GET axi3_parts 0 axi3_name)
  list(GET axi3_parts 1 axi3_mode)
  add_test(NAME axi3_${axi3_name}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi3.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/axi3_${axi3_name}
      --mode ${axi3_mode})
  set_tests_properties(axi3_${axi3_name} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;axi3;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Exclusive access (AXI A7.2): network monitors, target mode and the default.
foreach(mode IN ITEMS graph wide funnel split split_none)
  add_test(NAME exclusive_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_exclusive.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/exclusive_${mode}
      --mode ${mode})
  set_tests_properties(exclusive_${mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Exclusive access: a read that overtakes a held write, and clearing ranges.
foreach(mode IN ITEMS graph wide)
  add_test(NAME exclusive_order_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_exclusive_order.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/exclusive_order_${mode}
      --mode ${mode})
  set_tests_properties(exclusive_order_${mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Multiple outstanding bursts: per-ID source tracking and multi-burst AXI4 targets;
# the clocked modes cross two clock domains with the response tag in every FIFO.
foreach(mode IN ITEMS graph routes funnel first extremes many clocked clocked_slow clocked_fast
    clocked_extremes clocked_entry)
  add_test(NAME multi_outstanding_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_multi_outstanding.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/multi_outstanding_${mode}
      --mode ${mode})
  set_tests_properties(multi_outstanding_${mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
  # The same scenarios with a W stream running ahead of AW and random target
  # AW/W/AR backpressure.
  add_test(NAME multi_outstanding_${mode}_wahead
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_multi_outstanding.py
      --cli $<TARGET_FILE:nocloom_cli>
      --work ${CMAKE_CURRENT_BINARY_DIR}/multi_outstanding_${mode}_wahead --mode ${mode})
  set_tests_properties(multi_outstanding_${mode}_wahead PROPERTIES TIMEOUT 1800
    LABELS "rtl;axi4;focused" ENVIRONMENT NOCLOOM_MO_W_AHEAD=1
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# AXI4-Lite sources with shared read/write capacity: one ordering key per direction;
# with bounded packets, next to exclusive targets and at multi-transaction outlets.
foreach(mode IN ITEMS graph routes funnel mixed wide64 bounded exclusive multi_target clocked
    clocked_bounded perwidth)
  add_test(NAME lite_outstanding_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_lite_outstanding.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/lite_outstanding_${mode}
      --mode ${mode})
  set_tests_properties(lite_outstanding_${mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
# Exclusive access with several bursts in flight per source: contention,
# deterministic outcomes next to other IDs' traffic, and reset mid-flight.
foreach(mode IN ITEMS graph funnel)
  add_test(NAME exclusive_outstanding_${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_exclusive_outstanding.py
      --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/exclusive_outstanding_${mode}
      --mode ${mode})
  set_tests_properties(exclusive_outstanding_${mode} PROPERTIES TIMEOUT 1800 LABELS "rtl;axi4;focused"
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
add_executable(nocloom_treadle_fixture generate_treadle.cpp)
target_link_libraries(nocloom_treadle_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_treadle_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
foreach(case IN ITEMS dynamic formal)
  add_test(NAME arbiter_library_${case}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_treadle.py
      --fixture $<TARGET_FILE:nocloom_treadle_fixture>
      --work ${CMAKE_CURRENT_BINARY_DIR}/arbiter_library_${case} --case ${case})
  set_tests_properties(arbiter_library_${case} PROPERTIES TIMEOUT 3600 WORKING_DIRECTORY
    ${CMAKE_CURRENT_BINARY_DIR})
endforeach()
set_tests_properties(arbiter_library_dynamic PROPERTIES LABELS "arbiter;rtl;focused")
set_tests_properties(arbiter_library_formal PROPERTIES LABELS "arbiter;formal" TIMEOUT 7200)

# Schema 10 arbitration configuration; the networks under each policy run as
# verif_arbitration (tests/verif).
target_sources(nocloom_unit PRIVATE unit/arbitration.cpp unit/dependency_graph.cpp)
add_test(NAME arbitration_config COMMAND nocloom_unit "[arbitration-config]")
set_tests_properties(arbitration_config PROPERTIES TIMEOUT 120 LABELS "unit;config;arbiter")
target_sources(nocloom_unit PRIVATE unit/register_bank.cpp unit/service_target.cpp
  unit/fault_monitor.cpp)
add_test(NAME register_bank_plan COMMAND nocloom_unit "[register-bank]")
set_tests_properties(register_bank_plan PROPERTIES TIMEOUT 120 LABELS "unit;focused")
add_test(NAME service_target_plan COMMAND nocloom_unit "[service]")
set_tests_properties(service_target_plan PROPERTIES TIMEOUT 120 LABELS "unit;focused")
add_test(NAME fault_monitor_plan COMMAND nocloom_unit "[observer]")
set_tests_properties(fault_monitor_plan PROPERTIES TIMEOUT 120 LABELS "unit;focused")

add_test(NAME axi4_packetizer_multi_cdc
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_packetizer_multi_cdc.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_packetizer_multi_cdc)
set_tests_properties(axi4_packetizer_multi_cdc PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;focused;cdc"
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})


add_executable(nocloom_axi4_width_cut_multi_fixture generate_axi4_width_cut_multi.cpp)
target_link_libraries(nocloom_axi4_width_cut_multi_fixture PRIVATE nocloom_core)
target_include_directories(nocloom_axi4_width_cut_multi_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME axi4_width_cut_multi
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_axi4_width_cut_multi.py
    --fixture $<TARGET_FILE:nocloom_axi4_width_cut_multi_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/axi4_width_cut_multi)
set_tests_properties(axi4_width_cut_multi PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;focused"
  WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR})


foreach(mode funnel graph)
  add_test(NAME exclusive_outstanding_${mode}_configuration
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_exclusive_outstanding.py
      --cli $<TARGET_FILE:nocloom_cli> --mode ${mode} --configuration-only
      --work ${CMAKE_CURRENT_BINARY_DIR}/exclusive_outstanding_${mode}_configuration)
  set_tests_properties(exclusive_outstanding_${mode}_configuration PROPERTIES TIMEOUT 180 LABELS "unit;quick")
endforeach()

add_test(NAME conversion_configuration
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_conversion_configuration.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/conversion_configuration)
set_tests_properties(conversion_configuration PROPERTIES TIMEOUT 180 LABELS "unit;quick")

add_executable(nocloom_mesh_topology_unit unit/mesh_topology.cpp)
target_include_directories(nocloom_mesh_topology_unit PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_mesh_topology_unit PRIVATE nocloom_core Catch2::Catch2WithMain)
target_compile_options(nocloom_mesh_topology_unit PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall;-Wextra;-Wpedantic>)

add_executable(nocloom_tree_topology_unit unit/tree_topology.cpp)
target_include_directories(nocloom_tree_topology_unit PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_tree_topology_unit PRIVATE nocloom_core Catch2::Catch2WithMain)
add_test(NAME tree_topology_structure COMMAND nocloom_tree_topology_unit)
set_tests_properties(tree_topology_structure PROPERTIES TIMEOUT 60 LABELS "unit;topology")

add_executable(nocloom_link_pipeline_unit unit/link_pipeline.cpp)
target_include_directories(nocloom_link_pipeline_unit PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_link_pipeline_unit PRIVATE nocloom_core Catch2::Catch2WithMain)
add_test(NAME link_pipeline_structure COMMAND nocloom_link_pipeline_unit)
set_tests_properties(link_pipeline_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")
add_executable(nocloom_link_pipeline_fixture generate_link_pipeline.cpp)
target_include_directories(nocloom_link_pipeline_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_link_pipeline_fixture PRIVATE nocloom_core)
add_test(NAME link_pipeline_transactions
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_link_pipeline.py
    --fixture $<TARGET_FILE:nocloom_link_pipeline_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/link_pipeline_transactions)
set_tests_properties(link_pipeline_transactions PROPERTIES TIMEOUT 300 LABELS "rtl;focused" PROCESSORS 4)
add_executable(nocloom_qos_pipeline_fixture generate_qos_pipeline.cpp)
target_include_directories(nocloom_qos_pipeline_fixture PRIVATE ${PROJECT_SOURCE_DIR}/src)
target_link_libraries(nocloom_qos_pipeline_fixture PRIVATE nocloom_core)
add_test(NAME qos_pipeline_transactions
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_qos_pipeline.py
    --fixture $<TARGET_FILE:nocloom_qos_pipeline_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/qos_pipeline_transactions)
set_tests_properties(qos_pipeline_transactions PROPERTIES TIMEOUT 300 LABELS "rtl;focused" PROCESSORS 4)
add_test(NAME link_pipeline_network
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_link_pipeline.py
    --cli $<TARGET_FILE:nocloom_cli> --work ${CMAKE_CURRENT_BINARY_DIR}/link_pipeline_network)
set_tests_properties(link_pipeline_network PROPERTIES TIMEOUT 900 LABELS "rtl;axi4;focused" PROCESSORS 4)
add_test(NAME link_pipeline_config
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_link_pipeline.py
    --cli $<TARGET_FILE:nocloom_cli> --configuration-only
    --work ${CMAKE_CURRENT_BINARY_DIR}/link_pipeline_config)
set_tests_properties(link_pipeline_config PROPERTIES TIMEOUT 120 LABELS "config;quick")

add_test(NAME fixed_qos_arbiter
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/run_fixed_qos.py
    --fixture $<TARGET_FILE:nocloom_treadle_fixture> --cli $<TARGET_FILE:nocloom_cli>
    --network-fixture $<TARGET_FILE:nocloom_verif_fixture>
    --work ${CMAKE_CURRENT_BINARY_DIR}/fixed_qos_arbiter)
set_tests_properties(fixed_qos_arbiter PROPERTIES TIMEOUT 600 PROCESSORS 4 LABELS "rtl;arbiter;focused")
add_test(NAME fixed_qos_configuration COMMAND nocloom_unit "[fixed-qos]")
set_tests_properties(fixed_qos_configuration PROPERTIES TIMEOUT 120 LABELS "unit;config;arbiter")


add_test(NAME upsize_context_structure COMMAND nocloom_unit "[upsize-context]")
set_tests_properties(upsize_context_structure PROPERTIES TIMEOUT 60 LABELS "unit;quick")





add_test(NAME apb_cadence_structure COMMAND nocloom_unit "[apb-cadence]")
set_tests_properties(apb_cadence_structure PROPERTIES TIMEOUT 120 LABELS "unit;quick")

# Artifact bytes must not depend on the run or on the compiler: C++ leaves
# argument evaluation order unspecified and GCC and Clang choose differently
# (verif_examples and verif_determinism_compilers, tests/verif/cases.cmake).
# A candidate counts only when it builds and runs a C++20 program for this
# host: a cross toolchain earlier on PATH must not stand in for a host
# compiler. An explicit NOCLOOM_OTHER_CXX is probed first. A host without a
# second compiler opts out with NOCLOOM_DETERMINISM_COMPILERS=OFF: the case is
# then not registered and configure says so; every other tool stays required.
option(NOCLOOM_DETERMINISM_COMPILERS "Register verif_determinism_compilers (needs a second C++20 compiler)" ON)
set(nocloom_other_cxx "")
if(NOT NOCLOOM_DETERMINISM_COMPILERS)
  message(STATUS "NOT RUN: verif_determinism_compilers (NOCLOOM_DETERMINISM_COMPILERS=OFF)")
else()
  if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    set(nocloom_other_name clang++)
  elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    set(nocloom_other_name g++)
  endif()
  set(nocloom_candidates ${NOCLOOM_OTHER_CXX})
  if(nocloom_other_name)
    string(REPLACE ":" ";" nocloom_dirs "$ENV{PATH}")
    foreach(dir IN LISTS nocloom_dirs)
      if(dir AND EXISTS ${dir}/${nocloom_other_name} AND NOT IS_DIRECTORY ${dir}/${nocloom_other_name})
        list(APPEND nocloom_candidates ${dir}/${nocloom_other_name})
      endif()
    endforeach()
  endif()
  set(nocloom_probe ${CMAKE_CURRENT_BINARY_DIR}/other_cxx_probe)
  file(WRITE ${nocloom_probe}/probe.cpp "#include <string>\n#include <vector>\n"
    "int main() { std::vector<std::string> v{\"x\"}; return v.size() == 1 ? 0 : 1; }\n")
  foreach(candidate IN LISTS nocloom_candidates)
    execute_process(COMMAND ${candidate} -std=c++20 ${nocloom_probe}/probe.cpp -o ${nocloom_probe}/probe
      RESULT_VARIABLE built OUTPUT_QUIET ERROR_QUIET)
    if(built EQUAL 0)
      execute_process(COMMAND ${nocloom_probe}/probe RESULT_VARIABLE ran OUTPUT_QUIET ERROR_QUIET)
      if(ran EQUAL 0)
        set(nocloom_other_cxx ${candidate})
        break()
      endif()
    endif()
    message(STATUS "determinism_compilers: ${candidate} cannot build a program for this host")
  endforeach()
  if(NOT nocloom_other_cxx AND NOCLOOM_TEST_DEPS_REQUIRED)
    message(FATAL_ERROR "determinism_compilers requires a second C++ compiler that builds for this host "
      "(set NOCLOOM_OTHER_CXX, or NOCLOOM_DETERMINISM_COMPILERS=OFF to not register the case)")
  endif()
endif()

# The sample a small public CI runner finishes (ctest -L ci): every unit and quick test, and the
# cases named here. They cover each protocol family, width conversion, clock domain crossing,
# clock gating, error logging and the installed package. The three suite contracts stay out: they
# compare the free processors with the load average and refuse to start on a loaded runner.
set(NOCLOOM_CI_CASES cli install verif_examples
  axi4_public_installed multi_outstanding_graph multi_outstanding_clocked exclusive_graph
  verif_axi4_write_miss_answer verif_source_merge verif_qos_outstanding link_pipeline_network
  axi3_initiator_switch axi3_initiator_wide axi3_target
  lite_outstanding_graph lite_outstanding_clocked verif_lite_cut verif_lite_unaligned
  apb_initiator_graph apb_initiator_clocked verif_apb_target_widths verif_small_address
  ahb_initiator_switch ahb_initiator_wide ahb_initiator_clocked ahb_target_protocol
  width_adapter_roundtrip_32x16 verif_axi4_width_cut_128_32
  async_fifo_ratios related_clock_transactions graph_clocked_installed_endpoints)
# Two cases are registered on a condition.
if(TARGET nocloom_api_example)
  list(APPEND NOCLOOM_CI_CASES api_example mesh_topology_transactions)
endif()
if(NOCLOOM_DETERMINISM_COMPILERS)
  list(APPEND NOCLOOM_CI_CASES verif_determinism_compilers)
endif()
set(NOCLOOM_CI_EXCLUDED suite_runner_contract suite_adaptive_contract suite_probe_contract)
set_property(GLOBAL PROPERTY NOCLOOM_CI_MISSING "${NOCLOOM_CI_CASES}")
# Labels the sample among the tests of the calling directory.
function(nocloom_label_ci_sample)
  get_property(tests DIRECTORY PROPERTY TESTS)
  get_property(missing GLOBAL PROPERTY NOCLOOM_CI_MISSING)
  foreach(test IN LISTS tests)
    get_property(labels TEST ${test} PROPERTY LABELS)
    if(("unit" IN_LIST labels OR "quick" IN_LIST labels OR test IN_LIST NOCLOOM_CI_CASES)
        AND NOT test IN_LIST NOCLOOM_CI_EXCLUDED)
      set_property(TEST ${test} APPEND PROPERTY LABELS ci)
    endif()
    list(REMOVE_ITEM missing ${test})
  endforeach()
  set_property(GLOBAL PROPERTY NOCLOOM_CI_MISSING "${missing}")
endfunction()

# Unified verification environment; see tests/verif/CMakeLists.txt.
add_subdirectory(verif)
nocloom_label_ci_sample()
get_property(nocloom_ci_missing GLOBAL PROPERTY NOCLOOM_CI_MISSING)
if(nocloom_ci_missing)
  message(FATAL_ERROR "The CI sample names tests that are not registered: ${nocloom_ci_missing}")
endif()
