#include "core/hardware_builder.hpp"
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <nocloom/generation.hpp>
#include <string>
#include <variant>

int main(int argc, char **argv) {
  const std::string fault = argc == 2 ? argv[1] : "none";
  if (fault == "ir") {
    nocloom::detail::Builder b;
    b.ir.name = "nocloom_contract_probe";
    b.output("nl_data", b.constant(1, 0), "target:registers", "prdata");
    if (!nocloom::detail::verify(b.ir))
      return 3;
    b.ir.ports.front().width = 2;
    if (nocloom::detail::verify(b.ir)) {
      std::fputs("CHECK_FAIL HARDWARE_IR_VERIFY\n", stderr);
      return 1;
    }
    std::fputs("CHECK_PASS HARDWARE_IR_VERIFY\n", stdout);
    return 0;
  }
  nocloom::Definition d;
  d.name = "boundary";
  d.connectivity = nocloom::Connectivity{.all = true};
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"control", "main"}};
  d.initiators.front().protocol = nocloom::Protocol::Axi4Lite;
  d.initiators.front().data_width = 32;
  d.targets = {{"registers", "main", {{0, 4096}}}};
  d.targets.front().protocol = nocloom::Protocol::Apb4;
  d.targets.front().attributes = nocloom::TargetAttributes::amba();
  d.targets.front().data_width = 32;
  if (fault == "artifacts") {
    auto plan = nocloom::make_plan(d);
    if (!plan)
      return 3;
    auto artifacts = nocloom::generate(d, {static_cast<nocloom::Backend>(99)});
    if (artifacts) {
      std::fputs("CHECK_FAIL ARTIFACT_FAILURE_EMPTY\n", stderr);
      return 1;
    }
    try {
      (void)artifacts.value();
    } catch (const std::bad_variant_access &) {
      std::fputs("CHECK_PASS ARTIFACT_FAILURE_EMPTY\n", stdout);
      return 0;
    }
    return 4;
  }
  std::fputs("LIB_BEGIN\n", stderr);
  std::fflush(stderr);
  auto result = nocloom::generate(d);
  if (!result || result.value().size() != 6)
    return 3;
  d.targets.front().data_width = 48;
  auto rejected = nocloom::make_plan(d);
  // Fault controls exercise the process observer inside its monitored interval.
  // They are test probes, not alternative library implementations.
  if (fault == "io") {
    std::ofstream forbidden("unexpected-output");
    forbidden << "probe";
  } else if (fault == "print") {
    std::fputs("unexpected output\n", stdout);
    std::fflush(stdout);
  } else if (fault == "exit") {
    std::exit(0);
  }
  std::fputs("LIB_END\n", stderr);
  std::fflush(stderr);
  if (rejected)
    return 4;
  std::fputs("RETURNED\n", stdout);
}
