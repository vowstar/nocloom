#include "core/message.hpp"

#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <limits>
#include <nocloom/generation.hpp>

using nocloom::detail::describe_messages;
using nocloom::detail::MessageField;
using nocloom::detail::MessageFieldKind;

TEST_CASE("Message payload fields preserve their supported meanings", "[message]") {
  const auto result = describe_messages({32, 32, 16});
  REQUIRE(result);
  using Kind = MessageFieldKind;
  const std::vector<MessageField> request{{Kind::Address, 32},   {Kind::Write, 1},
                                          {Kind::WriteData, 32}, {Kind::ByteEnable, 4},
                                          {Kind::Protection, 3}, {Kind::Source, 4}};
  const std::vector<MessageField> response{
      {Kind::ReadData, 32}, {Kind::Result, 2}, {Kind::Source, 4}};
  CHECK(result.value().request == request);
  CHECK(result.value().response == response);
}

TEST_CASE("Message width examples distinguish design parameters from endpoint support",
          "[message]") {
  struct Example {
    unsigned address, data, sources;
    std::uint64_t request, response;
    unsigned bytes;
  };
  const Example examples[] = {{1, 32, 1, 41, 34, 4},
                              {32, 32, 16, 76, 38, 4},
                              {32, 32, 32, 77, 39, 4},
                              {40, 64, 3, 118, 68, 8},
                              {128, 128, 1, 276, 130, 16}};
  for (const auto &e : examples) {
    CAPTURE(e.address, e.data, e.sources);
    const auto result = describe_messages({e.address, e.data, e.sources});
    REQUIRE(result);
    CHECK(result.value().request_bits() == e.request);
    CHECK(result.value().response_bits() == e.response);
    const auto lane =
        std::find_if(result.value().request.begin(), result.value().request.end(),
                     [](const auto &f) { return f.kind == MessageFieldKind::ByteEnable; });
    REQUIRE(lane != result.value().request.end());
    CHECK(lane->width == e.bytes);
  }
}

TEST_CASE("Source encoding covers every source without zero width signals", "[message]") {
  for (unsigned count = 1; count <= 33; ++count) {
    CAPTURE(count);
    const auto result = describe_messages({32, 32, count});
    REQUIRE(result);
    for (const auto *fields : {&result.value().request, &result.value().response}) {
      for (const auto &field : *fields)
        CHECK(field.width > 0);
      const auto source = std::find_if(fields->begin(), fields->end(), [](const auto &f) {
        return f.kind == MessageFieldKind::Source;
      });
      if (count == 1) {
        CHECK(source == fields->end());
      } else {
        REQUIRE(source != fields->end());
        const auto capacity = std::uint64_t{1} << source->width;
        CHECK(capacity >= count);
        CHECK(capacity / 2 < count);
      }
    }
  }
}

TEST_CASE("Message description rejects invalid parameters with no value", "[message]") {
  const auto invalid = describe_messages({0, 24, 0});
  REQUIRE_FALSE(invalid);
  REQUIRE(invalid.diagnostics().size() == 3);
  CHECK(invalid.diagnostics()[0].field == "message.address_bits");
  CHECK(invalid.diagnostics()[1].field == "message.data_bits");
  CHECK(invalid.diagnostics()[2].field == "message.source_count");
  REQUIRE_THROWS_AS(invalid.value(), std::bad_variant_access);
  for (unsigned data : {0U, 1U, 2U, 4U, 31U, 33U, 48U, 96U}) {
    const auto result = describe_messages({40, data, 1});
    REQUIRE_FALSE(result);
    REQUIRE(result.diagnostics().size() == 1);
    CHECK(result.diagnostics().front().code == "NCL_MESSAGE_DATA_WIDTH");
  }
}

TEST_CASE("Message totals do not truncate at the parameter integer width", "[message]") {
  const auto limit = std::numeric_limits<unsigned>::max();
  const auto data = (limit / 2) + 1;
  const auto result = describe_messages({limit, data, limit});
  REQUIRE(result);
  CHECK(result.value().request_bits() > limit);
  CHECK(result.value().response_bits() > data);
  REQUIRE(result.value().request.back().kind == MessageFieldKind::Source);
  CHECK(result.value().request.back().width == std::numeric_limits<unsigned>::digits);
}

TEST_CASE("Burst occupancy can encode every admitted full beat", "[message]") {
  using namespace nocloom::detail;
  for (unsigned data : {32U, 64U, 128U, 256U, 512U, 1024U}) {
    const unsigned expected = data == 1024 ? 8 : data == 512 ? 7 : 6;
    CHECK(burst_count_bits(data) == expected);
    for (const auto &fields : {burst_request_fields(data, {4, 8, true, 40}, 2),
                               burst_response_fields(data, {4, 8, true, 40}, 2)}) {
      const auto it =
          std::ranges::find(fields, std::string("count"), &std::pair<std::string, unsigned>::first);
      REQUIRE(it != fields.end());
      CHECK(it->second == expected);
      CHECK((1U << expected) > data / 8);
    }
  }
}
