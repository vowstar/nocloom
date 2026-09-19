cmake_minimum_required(VERSION 3.24)
project(nocloom_suite_runner_contract NONE)
enable_testing()
find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
add_test(NAME suite_runner_contract
  COMMAND ${Python3_EXECUTABLE} -B ${CMAKE_CURRENT_LIST_DIR}/../test_suite_runner.py -v)
set_tests_properties(suite_runner_contract PROPERTIES TIMEOUT 60 LABELS "unit;suite-runner")
add_test(NAME suite_history_contract
  COMMAND ${Python3_EXECUTABLE} -B ${CMAKE_CURRENT_LIST_DIR}/../test_suite_history.py -v)
set_tests_properties(suite_history_contract PROPERTIES TIMEOUT 60 LABELS "unit;suite-history")
add_test(NAME suite_adaptive_contract
  COMMAND ${Python3_EXECUTABLE} -B ${CMAKE_CURRENT_LIST_DIR}/../test_suite_adaptive.py -v)
set_tests_properties(suite_adaptive_contract PROPERTIES TIMEOUT 60 LABELS "unit;suite-adaptive")
add_test(NAME suite_probe_contract
  COMMAND ${Python3_EXECUTABLE} -B ${CMAKE_CURRENT_LIST_DIR}/../test_suite_probe.py -v)
set_tests_properties(suite_probe_contract PROPERTIES TIMEOUT 60 LABELS "unit;suite-probe")
