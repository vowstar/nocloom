cmake_minimum_required(VERSION 3.24)
project(nocloom VERSION 0.2.0 LANGUAGES CXX)
include(GNUInstallDirs)
include(CMakePackageConfigHelpers)
option(NOCLOOM_BUILD_CONFIG "Build YAML and file adapters" ON)
option(NOCLOOM_BUILD_CLI "Build CLI" ${PROJECT_IS_TOP_LEVEL})
option(NOCLOOM_BUILD_TESTS "Build tests" ${PROJECT_IS_TOP_LEVEL})
option(NOCLOOM_BUILD_EXAMPLES "Build API example" ${PROJECT_IS_TOP_LEVEL})
option(NOCLOOM_TEST_DEPS_REQUIRED "Fail when required verification tools are missing" ON)
if(PROJECT_IS_TOP_LEVEL)
  set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
  # Without an explicit build type the compiler runs unoptimized and the
  # compute-bound unit tests exceed their bounds; default to Release.
  get_property(NOCLOOM_MULTI_CONFIG GLOBAL PROPERTY GENERATOR_IS_MULTI_CONFIG)
  if(NOT CMAKE_BUILD_TYPE AND NOT NOCLOOM_MULTI_CONFIG)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type (default Release)" FORCE)
  endif()
  include(CTest)
endif()
if(NOCLOOM_BUILD_CLI AND NOT NOCLOOM_BUILD_CONFIG)
  message(FATAL_ERROR "The CLI requires NOCLOOM_BUILD_CONFIG")
endif()
find_package(nlohmann_json 3.11.3 CONFIG REQUIRED)
add_library(nocloom_core STATIC src/core/validation.cpp src/core/hardware.cpp src/core/message.cpp
  src/backends/output_license.cpp
  src/backends/generation.cpp src/backends/verilog/emitter.cpp src/backends/ipxact/component.cpp
  src/backends/address_table.cpp src/backends/constraints/cdc_sdc.cpp)
add_library(nocloom::core ALIAS nocloom_core)
set_target_properties(nocloom_core PROPERTIES EXPORT_NAME core)
# The generated files name this version. A non-empty suffix marks an unreleased build.
set(NOCLOOM_VERSION_SUFFIX "-dev")
target_compile_definitions(nocloom_core
  PRIVATE NOCLOOM_VERSION="${PROJECT_VERSION}${NOCLOOM_VERSION_SUFFIX}")
target_compile_features(nocloom_core PUBLIC cxx_std_20)
target_include_directories(nocloom_core PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}> PRIVATE src)
target_link_libraries(nocloom_core PRIVATE $<BUILD_INTERFACE:nlohmann_json::nlohmann_json>)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(nocloom_core PRIVATE -Wall -Wextra -Wpedantic)
endif()
install(TARGETS nocloom_core EXPORT nocloomCoreTargets ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(EXPORT nocloomCoreTargets NAMESPACE nocloom:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/nocloom)
add_library(nocloom INTERFACE)
add_library(nocloom::nocloom ALIAS nocloom)
target_link_libraries(nocloom INTERFACE nocloom_core)
if(NOCLOOM_BUILD_CONFIG)
  find_package(yaml-cpp 0.8.0 CONFIG REQUIRED)
  add_library(nocloom_config STATIC src/config/yaml.cpp src/config/shorthand.cpp)
  add_library(nocloom::config ALIAS nocloom_config)
  set_target_properties(nocloom_config PROPERTIES EXPORT_NAME config)
  target_link_libraries(nocloom_config PUBLIC nocloom_core PRIVATE yaml-cpp::yaml-cpp)
  add_library(nocloom_io STATIC src/io/files.cpp)
  add_library(nocloom::io ALIAS nocloom_io)
  set_target_properties(nocloom_io PROPERTIES EXPORT_NAME io)
  target_link_libraries(nocloom_io PUBLIC nocloom_config)
  target_link_libraries(nocloom INTERFACE nocloom_io)
  install(TARGETS nocloom_config nocloom_io EXPORT nocloomConfigTargets ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
  install(EXPORT nocloomConfigTargets NAMESPACE nocloom:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/nocloom)
endif()
if(NOCLOOM_BUILD_CLI)
  find_package(CLI11 2.4.1 CONFIG REQUIRED)
  add_executable(nocloom_cli cli/main.cpp cli/diff.cpp)
  set_target_properties(nocloom_cli PROPERTIES OUTPUT_NAME nocloom)
  target_link_libraries(nocloom_cli PRIVATE nocloom_io CLI11::CLI11 nlohmann_json::nlohmann_json)
  install(TARGETS nocloom_cli RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
if(NOCLOOM_BUILD_EXAMPLES)
  add_executable(nocloom_api_example examples/generate_from_definition.cpp)
  target_link_libraries(nocloom_api_example PRIVATE nocloom_core)
  add_executable(nocloom_feature_definitions_example examples/generate_feature_definitions.cpp)
  target_link_libraries(nocloom_feature_definitions_example PRIVATE nocloom_core)
  add_executable(nocloom_tree_example examples/generate_tree.cpp)
  target_link_libraries(nocloom_tree_example PRIVATE nocloom_core)
  add_executable(nocloom_mesh_example examples/generate_mesh.cpp)
  target_link_libraries(nocloom_mesh_example PRIVATE nocloom_core)
  target_compile_options(nocloom_mesh_example PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall;-Wextra;-Wpedantic>)
  add_executable(nocloom_large_tree_example examples/generate_large_tree.cpp)
  target_link_libraries(nocloom_large_tree_example PRIVATE nocloom_core)
endif()
foreach(target IN ITEMS nocloom_core nocloom_config nocloom_io nocloom_cli nocloom_api_example)
  if(TARGET ${target})
    set_target_properties(${target} PROPERTIES POSITION_INDEPENDENT_CODE ON)
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang" AND NOT target STREQUAL "nocloom_core")
      target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
    endif()
  endif()
endforeach()
install(FILES LICENSE NOTICE DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom)
install(DIRECTORY LICENSES DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom)
install(FILES docs/attribution.md DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom/docs)
install(FILES third_party/licenses/nlohmann_json.txt third_party/licenses/Catch2.txt
  DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom/third_party/licenses)
if(NOCLOOM_BUILD_CONFIG)
  install(FILES third_party/licenses/yaml-cpp.txt
    DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom/third_party/licenses)
endif()
if(NOCLOOM_BUILD_CLI)
  install(FILES third_party/licenses/CLI11.txt
    DESTINATION ${CMAKE_INSTALL_DATADIR}/nocloom/third_party/licenses)
endif()
install(DIRECTORY include/nocloom DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
configure_package_config_file(cmake/nocloomConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/nocloomConfig.cmake INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/nocloom)
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/nocloomConfigVersion.cmake
  VERSION ${PROJECT_VERSION} COMPATIBILITY SameMajorVersion)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/nocloomConfig.cmake
  ${CMAKE_CURRENT_BINARY_DIR}/nocloomConfigVersion.cmake DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/nocloom)
if(PROJECT_IS_TOP_LEVEL)
  # The format and analysis targets serve development. A build with tests requires both tools.
  # A build without tests adds the target of each tool it finds.
  set(NOCLOOM_DEV_TOOL_REQUIRED "")
  if(NOCLOOM_BUILD_TESTS AND BUILD_TESTING)
    set(NOCLOOM_DEV_TOOL_REQUIRED REQUIRED)
  endif()
  find_program(NOCLOOM_CLANG_FORMAT clang-format ${NOCLOOM_DEV_TOOL_REQUIRED})
  if(NOCLOOM_CLANG_FORMAT)
    file(GLOB_RECURSE NOCLOOM_FORMAT_SOURCES CONFIGURE_DEPENDS include/*.hpp src/*.hpp src/*.cpp
      cli/*.cpp tests/*.cpp examples/*.cpp)
    add_custom_target(clang-format COMMAND ${NOCLOOM_CLANG_FORMAT} -i ${NOCLOOM_FORMAT_SOURCES} VERBATIM)
  endif()
  find_program(NOCLOOM_CPPCHECK cppcheck ${NOCLOOM_DEV_TOOL_REQUIRED})
  if(NOCLOOM_CPPCHECK)
    add_custom_target(cppcheck COMMAND ${NOCLOOM_CPPCHECK}
      --project=${CMAKE_CURRENT_BINARY_DIR}/compile_commands.json --check-level=exhaustive
      --enable=warning,style,performance,portability --error-exitcode=1
      --suppress=missingIncludeSystem -j16 VERBATIM)
  endif()
endif()
if(NOCLOOM_BUILD_TESTS AND BUILD_TESTING)
  enable_testing()
  add_subdirectory(tests)
endif()

target_sources(nocloom_core PRIVATE src/core/ir_component.cpp)

target_sources(nocloom_core PRIVATE src/core/cross_domain.cpp)

target_sources(nocloom_core PRIVATE src/core/async_fifo.cpp src/core/related_clock.cpp)

target_sources(nocloom_core PRIVATE src/core/switch_graph.cpp src/core/tree_topology.cpp
  src/core/mesh_topology.cpp)

target_sources(nocloom_core PRIVATE src/core/exchange_composition.cpp src/core/exchange_clocking.cpp
  src/core/dependency_graph.cpp src/core/network_plan.cpp src/core/network_plan_profiles.cpp src/core/plan_obligations.cpp
  src/core/compose_checks.cpp src/core/capability.cpp)
target_sources(nocloom_core PRIVATE src/core/treadle.cpp)
target_sources(nocloom_core PRIVATE src/core/register_bank.cpp src/core/service_outlet.cpp
                                     src/core/error_logger.cpp)

target_sources(nocloom_core PRIVATE src/core/width_adapter.cpp)

target_sources(nocloom_core PRIVATE src/core/axi4_initiator.cpp src/core/axi4_output.cpp)
target_sources(nocloom_core PRIVATE src/core/axi4_outlet.cpp)
target_sources(nocloom_core PRIVATE src/core/ahb_outlet.cpp)
target_sources(nocloom_core PRIVATE src/core/exclusive_monitor.cpp)
target_sources(nocloom_core PRIVATE src/core/axi4_width_clamp.cpp)
target_sources(nocloom_core PRIVATE src/core/axi4_packetizer.cpp)
target_sources(nocloom_core PRIVATE src/core/axi4_upsize.cpp)
