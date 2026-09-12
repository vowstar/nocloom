#include "message.hpp"

#include <bit>
#include <numeric>

namespace nocloom::detail {
namespace {
std::uint64_t payload_bits(const std::vector<MessageField> &fields) {
  return std::accumulate(
      fields.begin(), fields.end(), std::uint64_t{0},
      [](std::uint64_t total, const MessageField &field) { return total + field.width; });
}
} // namespace

std::uint64_t MessageLayout::request_bits() const { return payload_bits(request); }
std::uint64_t MessageLayout::response_bits() const { return payload_bits(response); }

// The Weft message format derives payload widths independently of endpoint capabilities.
Result<MessageLayout> describe_messages(MessageParameters parameters) {
  std::vector<Diagnostic> errors;
  if (parameters.address_bits == 0)
    errors.push_back({"NCL_MESSAGE_ADDRESS_WIDTH",
                      "message.address_bits",
                      "Message address width must be positive",
                      {},
                      {}});
  if (parameters.data_bits < 8 || !std::has_single_bit(parameters.data_bits))
    errors.push_back({"NCL_MESSAGE_DATA_WIDTH",
                      "message.data_bits",
                      "Message data width must be a power of two and at least 8",
                      {},
                      {}});
  if (parameters.source_count == 0)
    errors.push_back({"NCL_MESSAGE_SOURCE_COUNT",
                      "message.source_count",
                      "Message source count must be positive",
                      {},
                      {}});
  if (!errors.empty())
    return Result<MessageLayout>::failure(std::move(errors));

  using Kind = MessageFieldKind;
  MessageLayout layout{{{Kind::Address, parameters.address_bits},
                        {Kind::Write, 1},
                        {Kind::WriteData, parameters.data_bits},
                        {Kind::ByteEnable, parameters.data_bits / 8},
                        {Kind::Protection, 3}},
                       {{Kind::ReadData, parameters.data_bits}, {Kind::Result, 2}}};
  const auto source_bits = static_cast<unsigned>(std::bit_width(parameters.source_count - 1));
  if (source_bits != 0) {
    layout.request.push_back({Kind::Source, source_bits});
    layout.response.push_back({Kind::Source, source_bits});
  }
  return Result<MessageLayout>::success(std::move(layout));
}

unsigned burst_count_bits(unsigned data_bits) {
  const auto bits = static_cast<unsigned>(std::bit_width(data_bits / 8));
  return bits > 6 ? bits : 6;
}

std::vector<std::pair<std::string, unsigned>>
burst_request_fields(unsigned data_bits, BurstProfile profile, unsigned source_bits) {
  std::vector<std::pair<std::string, unsigned>> fields;
  fields.emplace_back("address", profile.address_bits);
  fields.emplace_back("write", 1);
  fields.emplace_back("protection", 3);
  fields.emplace_back("length", profile.len_bits);
  fields.emplace_back("size", 3);
  fields.emplace_back("burst", 2);
  fields.emplace_back("cache", 4);
  if (profile.exclusive)
    fields.emplace_back("lock", 1);
  if (profile.user_bits)
    fields.emplace_back("user", profile.user_bits);
  if (profile.record_bits)
    fields.emplace_back("record", profile.record_bits);
  if (profile.security_bits)
    fields.emplace_back("security", profile.security_bits);
  if (profile.start_offset_bits)
    fields.emplace_back("start_offset", profile.start_offset_bits);
  else if (profile.start_address)
    fields.emplace_back("start_address", profile.address_bits);
  if (profile.tag_bits)
    fields.emplace_back("tag", profile.tag_bits);
  if (profile.id_bits)
    fields.emplace_back("id", profile.id_bits);
  if (source_bits)
    fields.emplace_back("source", source_bits);
  if (profile.path_bits)
    fields.emplace_back("path", profile.path_bits);
  if (profile.level_bits)
    fields.emplace_back("level", profile.level_bits);
  if (profile.target_bits)
    fields.emplace_back("target", profile.target_bits);
  if (profile.raise)
    fields.emplace_back("raise", 1);
  fields.emplace_back("count", burst_count_bits(data_bits));
  fields.emplace_back("ends", 1);
  fields.emplace_back("wrap_end", 1);
  fields.emplace_back("error", 1);
  fields.emplace_back("first", 1);
  fields.emplace_back("last", 1);
  fields.emplace_back("data", data_bits);
  fields.emplace_back("be", data_bits / 8);
  return fields;
}

std::vector<std::pair<std::string, unsigned>>
burst_response_fields(unsigned data_bits, BurstProfile profile, unsigned source_bits) {
  std::vector<std::pair<std::string, unsigned>> fields;
  fields.emplace_back("address", profile.address_bits);
  fields.emplace_back("result", 2);
  if (profile.exclusive)
    fields.emplace_back("exclusive_okay", 1);
  if (profile.response_user_bits)
    fields.emplace_back("user", profile.response_user_bits);
  if (profile.tag_bits)
    fields.emplace_back("tag", profile.tag_bits);
  if (profile.id_bits)
    fields.emplace_back("id", profile.id_bits);
  if (source_bits)
    fields.emplace_back("source", source_bits);
  if (profile.path_bits)
    fields.emplace_back("path", profile.path_bits);
  if (profile.response_level_bits)
    fields.emplace_back("level", profile.response_level_bits);
  fields.emplace_back("count", burst_count_bits(data_bits));
  fields.emplace_back("error", 1);
  fields.emplace_back("first", 1);
  fields.emplace_back("last", 1);
  fields.emplace_back("data", data_bits);
  return fields;
}

} // namespace nocloom::detail
