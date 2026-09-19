#include "core/message.hpp"

#include <algorithm>
#include <iostream>
#include <limits>
#include <string>

using namespace nocloom;
using namespace nocloom::detail;

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  const std::string name = argv[1];
  auto field = [](const std::vector<MessageField> &fields, MessageFieldKind kind) {
    const auto it =
        std::find_if(fields.begin(), fields.end(), [&](auto f) { return f.kind == kind; });
    return it == fields.end() ? 0U : it->width;
  };
  bool okay = false;
  using K = MessageFieldKind;
  const auto description = describe_messages({40, 64, 3});
  if (!description)
    return 3;
  const auto &m = description.value();
  if (name == "MESSAGE_FIELD_SET") {
    const std::vector<MessageField> req{{K::Address, 40},   {K::Write, 1},      {K::WriteData, 64},
                                        {K::ByteEnable, 8}, {K::Protection, 3}, {K::Source, 2}};
    const std::vector<MessageField> rsp{{K::ReadData, 64}, {K::Result, 2}, {K::Source, 2}};
    okay = m.request == req && m.response == rsp;
  } else if (name == "MESSAGE_BE_DERIVED") {
    okay = field(m.request, K::ByteEnable) == 8 &&
           field(describe_messages({40, 128, 1}).value().request, K::ByteEnable) == 16;
  } else if (name == "MESSAGE_SOURCE_WIDTH") {
    okay = true;
    for (unsigned n : {2U, 3U, 16U, 32U, 33U}) {
      const auto d = describe_messages({32, 32, n});
      const auto w = field(d.value().request, K::Source), r = field(d.value().response, K::Source);
      okay = okay && w == r && w > 0 && w < 32 && (std::uint64_t{1} << w) >= n &&
             (std::uint64_t{1} << (w - 1)) < n;
    }
  } else if (name == "MESSAGE_SOURCE_OMISSION") {
    const auto d = describe_messages({32, 32, 1});
    okay = d.value().request.size() == 5 && d.value().response.size() == 2;
  } else if (name == "MESSAGE_TOTAL_WIDTH") {
    const auto limit = std::numeric_limits<unsigned>::max();
    const auto d = describe_messages({limit, limit / 2 + 1, 1});
    okay = d.value().request_bits() > limit;
  } else if (name == "MESSAGE_FIXED_WIDTH.WRITE")
    okay = field(m.request, K::Write) == 1;
  else if (name == "MESSAGE_FIXED_WIDTH.PROTECTION")
    okay = field(m.request, K::Protection) == 3;
  else if (name == "MESSAGE_FIXED_WIDTH.RESULT")
    okay = field(m.response, K::Result) == 2;
  else if (name == "MESSAGE_DATA_ADDRESS_WIDTH.ADDRESS")
    okay = field(m.request, K::Address) == 40;
  else if (name == "MESSAGE_DATA_ADDRESS_WIDTH.WRITE_DATA")
    okay = field(m.request, K::WriteData) == 64;
  else if (name == "MESSAGE_DATA_ADDRESS_WIDTH.READ_DATA")
    okay = field(m.response, K::ReadData) == 64;
  else if (name.starts_with("MESSAGE_PARAMETER_FAILURE.")) {
    MessageParameters p{32, 32, 1};
    std::string diagnostic;
    if (name.ends_with("ADDRESS") || name.ends_with("NO_VALUE")) {
      p.address_bits = 0;
      diagnostic = "NCL_MESSAGE_ADDRESS_WIDTH";
    } else if (name.ends_with("SOURCE")) {
      p.source_count = 0;
      diagnostic = "NCL_MESSAGE_SOURCE_COUNT";
    } else if (name.ends_with("DATA_MIN")) {
      p.data_bits = 4;
      diagnostic = "NCL_MESSAGE_DATA_WIDTH";
    } else if (name.ends_with("DATA_POWER")) {
      p.data_bits = 48;
      diagnostic = "NCL_MESSAGE_DATA_WIDTH";
    }
    const auto d = describe_messages(p);
    okay = !d && d.diagnostics().size() == 1 && d.diagnostics().front().code == diagnostic;
    if (name.ends_with("NO_VALUE")) {
      bool threw = false;
      try {
        (void)d.value();
      } catch (const std::bad_variant_access &) {
        threw = true;
      }
      okay = okay && threw;
    }
  } else
    return 2;
  std::cout << (okay ? "CHECK_PASS " : "CHECK_FAIL ") << name << '\n';
  return okay ? 0 : 1;
}
