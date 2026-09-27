#include "core/register_bank.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>

using namespace nocloom::detail;
namespace {
RegisterBankPlan tiny() {
  return {"tiny",
          3,
          {{"a", 0, 0, 8, RegisterAccess::Control, 1}, {"b", 1, 4, 4, RegisterAccess::Pulse}}};
}
const RegisterField &named(const RegisterBankPlan &plan, const std::string &name) {
  const auto it = std::ranges::find(plan.fields, name, &RegisterField::name);
  REQUIRE(it != plan.fields.end());
  return *it;
}
bool rejected(const RegisterBankPlan &plan) {
  const auto checked = check_register_bank(plan);
  if (checked)
    return false;
  REQUIRE(checked.diagnostics().front().code == "NCL_INTERNAL_PLAN");
  return !lower_register_bank(plan);
}
} // namespace

TEST_CASE("A register plan lowers to a bank with one port per outward field", "[register-bank]") {
  const auto bank = lower_register_bank(tiny());
  REQUIRE(bank);
  const auto &ports = bank.value().ir.ports;
  for (const auto *name : {"nl_field_a", "nl_field_b", "nl_request_ready", "nl_response_data"})
    CHECK(std::ranges::find(ports, name, &Port::name) != ports.end());
  CHECK(bank.value().ir.name == "nocloom_register_bank_tiny");
}

TEST_CASE("A malformed register plan is rejected", "[register-bank]") {
  auto plan = tiny();
  plan.route = 1U << 11;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields.clear();
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[1].name = "a";
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[0].width = 0;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[0].lsb = 28;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[0].value = 256;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[1].value = 1;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[1].word = 0;
  plan.fields[1].lsb = 4;
  CHECK(rejected(plan));
  plan = tiny();
  plan.fields[0].word = 1U << 18;
  CHECK(rejected(plan));
}

TEST_CASE("The error logger table follows the record shape", "[register-bank]") {
  const auto narrow = error_logger_bank("log", 0, {3, 32, 0, 0, 2, 0});
  REQUIRE(check_register_bank(narrow));
  CHECK(std::ranges::find(narrow.fields, "record_user_word0", &RegisterField::name) ==
        narrow.fields.end());
  CHECK(std::ranges::find(narrow.fields, "record_address_high", &RegisterField::name) ==
        narrow.fields.end());
  const auto wide = error_logger_bank("log", 0, {12, 46, 72, 8, 8, 3});
  REQUIRE(check_register_bank(wide));
  CHECK(named(wide, "record_address_high").width == 14);
  CHECK(named(wide, "record_user_word1").width == 32);
  CHECK(named(wide, "record_user_word2").width == 8);
  CHECK(named(wide, "record_user_word2").word == named(wide, "record_user_word1").word + 1);
  // The identity follows the table and nothing else of it is identity.
  CHECK(named(narrow, "identity_checksum").value != named(wide, "identity_checksum").value);
  CHECK(named(narrow, "identity_checksum").value ==
        named(error_logger_bank("other", 7, {3, 32, 0, 0, 2, 0}), "identity_checksum").value);
  CHECK(named(narrow, "identity_type").value == 1);
  // A given revision replaces the generator version; the layout and the identity stay.
  const auto given = error_logger_bank("log", 0, {3, 32, 0, 0, 2, 0}, 0xA5C33CU);
  REQUIRE(check_register_bank(given));
  CHECK(named(given, "revision_generator").value == 0xA5C33CU);
  CHECK(named(given, "identity_checksum").value == named(narrow, "identity_checksum").value);
  CHECK(given.fields.size() == narrow.fields.size());
  CHECK_FALSE(check_register_bank(error_logger_bank("log", 0, {3, 32, 0, 0, 2, 0}, 0x1000000U)));
  CHECK(lower_register_bank(wide));
}
