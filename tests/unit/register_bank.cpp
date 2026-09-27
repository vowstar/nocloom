#include "core/register_bank.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <numeric>
#include <tuple>

using namespace nocloom::detail;
namespace {
// Two words under key 3.
RegisterBankPlan tiny() {
  return {
      "tiny", 3, {{"a", 0, 0, 8, RegisterAccess::Rw, 1}, {"b", 1, 4, 4, RegisterAccess::Rw1c}}, 3};
}
const RegisterField &named(const RegisterBankPlan &plan, const std::string &name) {
  const auto it = std::ranges::find(plan.fields, name, &RegisterField::name);
  REQUIRE(it != plan.fields.end());
  return *it;
}
bool absent(const RegisterBankPlan &plan, const std::string &name) {
  return std::ranges::find(plan.fields, name, &RegisterField::name) == plan.fields.end();
}
bool rejected(const RegisterBankPlan &plan) {
  const auto checked = check_register_bank(plan);
  if (checked)
    return false;
  REQUIRE(checked.diagnostics().front().code == "NCL_INTERNAL_PLAN");
  return !lower_register_bank(plan);
}
// The message of a rejected plan, or nothing.
std::string refusal(const RegisterBankPlan &plan) {
  const auto checked = check_register_bank(plan);
  return checked ? std::string{} : checked.diagnostics().front().message;
}
std::uint64_t word_value(const RegisterBankPlan &plan, unsigned word) {
  return std::accumulate(plan.fields.begin(), plan.fields.end(), std::uint64_t{0},
                         [word](std::uint64_t value, const RegisterField &f) {
                           return f.word == word && f.access == RegisterAccess::RoFixed
                                      ? value | f.value << f.lsb
                                      : value;
                         });
}
// The key and the register index fill the header word of a frame.
static_assert(1U << register_key_bits == 32 && 1U << register_index_bits == 16);
static_assert(register_key_bits + register_index_bits == register_frame_word_bits);
// A record with every optional part at its widest.
constexpr ErrorRecordShape widest{74, 48, 256, 32, 15, 2, 32, 13, 13, 16};
// A record of an APB network without classes, flags or an AxID.
constexpr ErrorRecordShape narrow{3, 32, 0, 0, 2, 0, 1, 1, 1, 0};
} // namespace

TEST_CASE("A register plan lowers to a bank with one port per outward field", "[register-bank]") {
  const auto bank = lower_register_bank(tiny());
  REQUIRE(bank);
  const auto &ports = bank.value().ir.ports;
  for (const auto *name :
       {"nl_field_a", "nl_field_b", "nl_field_b_clear", "nl_request_ready", "nl_response_data"})
    CHECK(std::ranges::find(ports, name, &Port::name) != ports.end());
  CHECK(bank.value().ir.name == "nocloom_register_bank_tiny");
}

TEST_CASE("A malformed register plan is rejected", "[register-bank]") {
  auto plan = tiny();
  CHECK(check_register_bank(plan));
  // A key fills the five bits above the index of a header: 31 is the last.
  plan.key = 31;
  CHECK(check_register_bank(plan));
  CHECK(lower_register_bank(plan));
  plan.key = 32;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A key has 5 bits");
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
  // A field past the bank's words is not the bank's.
  plan = tiny();
  plan.fields[0].word = 2;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A field must fit one word of the bank");
  // A bank has at most 16 words, the indices 0 to 15 of a header.
  plan = tiny();
  plan.address_bits = 6;
  plan.fields[0].word = 15;
  CHECK(check_register_bank(plan));
  CHECK(lower_register_bank(plan));
  plan.fields[0].word = 16;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A field must fit one word of the bank");
  plan.address_bits = 7;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A bank spans at least a word and at most 16");
  plan.fields[0].word = 0;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A bank spans at least a word and at most 16");
  // One word is a bank; less is none.
  plan = tiny();
  plan.fields.pop_back();
  plan.address_bits = 2;
  CHECK(check_register_bank(plan));
  plan.address_bits = 1;
  CHECK(rejected(plan));
  CHECK(refusal(plan) == "A bank spans at least a word and at most 16");
}

TEST_CASE("Layout 2 of the error log bank has this table", "[register-bank]") {
  // Written out here and not taken from the generator: name, word, lsb, width, access.
  using A = RegisterAccess;
  using Row = std::tuple<std::string, unsigned, unsigned, unsigned, A>;
  std::vector<Row> layout2{
      {"capability_audit_flags", 0, 0, 1, A::RoFixed},
      {"capability_user_flags", 0, 1, 1, A::RoFixed},
      {"capability_target_user_flags", 0, 2, 1, A::RoFixed},
      {"capability_next_logger", 0, 3, 1, A::RoFixed},
      {"capability_given_revision", 0, 4, 1, A::RoFixed},
      {"logger_index", 0, 8, 5, A::RoFixed},
      {"log_index", 0, 16, 6, A::RoFixed},
      {"layout", 0, 24, 8, A::RoFixed},
      {"record_held", 1, 0, 1, A::Rw1c},
      {"record_overflow", 1, 1, 1, A::Rw1c},
      {"record_class", 1, 4, 2, A::Ro},
      {"alarm_enable", 1, 8, 1, A::Rw},
      {"wait_when_full", 1, 9, 1, A::Rw},
      {"record_write", 1, 12, 1, A::Ro},
      {"record_wrap", 1, 13, 1, A::Ro},
      {"record_exclusive", 1, 14, 1, A::Ro},
      {"record_address_partial", 1, 15, 1, A::Ro},
      {"record_sequence", 1, 16, 16, A::Ro},
      {"record_address_low", 2, 0, 32, A::Ro},
      {"record_address_high", 3, 0, 16, A::Ro},
      {"record_bytes", 3, 16, 16, A::Ro},
      {"record_source_path", 4, 0, 13, A::Ro},
      {"record_target_path", 4, 16, 13, A::Ro},
      {"record_region_index", 5, 0, 32, A::Ro},
      {"record_audit_flags", 6, 0, 32, A::Ro},
      {"revision", 7, 0, 32, A::RoFixed},
  };
  for (unsigned k = 0; k < 8; ++k)
    layout2.emplace_back("record_user_flags" + std::to_string(k), 8 + k, 0, 32, A::Ro);
  const auto bank = error_logger_bank("log", widest, {63, 31, true, true, 0xFFFFFFFFU});
  REQUIRE(check_register_bank(bank));
  // The last logger of a chain has the last key.
  CHECK(bank.key == 31);
  std::vector<Row> stated(bank.fields.size());
  std::ranges::transform(bank.fields, stated.begin(), [](const RegisterField &f) {
    return Row{f.name, f.word, f.lsb, f.width, f.access};
  });
  std::ranges::sort(stated, {},
                    [](const Row &r) { return std::pair{std::get<1>(r), std::get<2>(r)}; });
  CHECK(stated == layout2);
  CHECK(bank.address_bits == 6);
  CHECK(word_value(bank, 0) == 0x023F1F1FU);
  CHECK(word_value(bank, 7) == 0xFFFFFFFFU);
  CHECK(lower_register_bank(bank));
}

TEST_CASE("The error logger table follows the record shape and the bank's place",
          "[register-bank]") {
  const auto plain = error_logger_bank("log", narrow);
  REQUIRE(check_register_bank(plain));
  CHECK(plain.key == 0);
  for (const auto *name : {"record_class", "record_address_high", "record_sequence",
                           "record_audit_flags", "record_user_flags0"})
    CHECK(absent(plain, name));
  CHECK(named(plain, "record_bytes").width == 3);
  CHECK(named(plain, "record_region_index").width == 1);
  // Layout 2, log 0, logger 0, no optional part.
  CHECK(word_value(plain, 0) == 0x02000000U);
  const ErrorRecordShape wide{12, 46, 72, 8, 8, 2, 3, 2, 2, 4};
  const auto placed = error_logger_bank("log", wide, {2, 3, true});
  REQUIRE(check_register_bank(placed));
  // The key is the logger's place in its chain, whatever the log.
  CHECK(placed.key == 3);
  for (unsigned logger = 0; logger < 32; ++logger) {
    const auto at = error_logger_bank("log", narrow, {logger % 4, logger});
    CHECK(check_register_bank(at));
    CHECK(at.key == logger);
    CHECK(named(at, "logger_index").value == logger);
  }
  CHECK(named(placed, "record_address_high").width == 14);
  CHECK(named(placed, "record_bytes").width == 9);
  CHECK(named(placed, "record_user_flags1").width == 32);
  CHECK(named(placed, "record_user_flags2").width == 8);
  CHECK(named(placed, "record_user_flags2").word == named(placed, "record_user_flags1").word + 1);
  // Log 2, logger 3, audit and user flags, another logger after it.
  CHECK(word_value(placed, 0) == 0x0202030BU);
  CHECK(named(error_logger_bank("log", wide, {0, 0, false, true}), "capability_target_user_flags")
            .value == 1);
  // A given revision takes the whole revision word and says so; nothing else changes.
  const auto given = error_logger_bank("log", narrow, {0, 0, false, false, 0xA5C33C5AU});
  REQUIRE(check_register_bank(given));
  CHECK(named(given, "revision").value == 0xA5C33C5AU);
  CHECK(word_value(given, 0) == 0x02000010U);
  CHECK(named(plain, "revision").value >> 24 == 0);
  CHECK(given.fields.size() == plain.fields.size());
  // A place outside the layout is no bank.
  CHECK_FALSE(check_register_bank(error_logger_bank("log", narrow, {64, 0})));
  CHECK_FALSE(check_register_bank(error_logger_bank("log", narrow, {0, 32})));
  CHECK(refusal(error_logger_bank("log", narrow, {0, 32})) == "A key has 5 bits");
  CHECK(lower_register_bank(placed));
}
