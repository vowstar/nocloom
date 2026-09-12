#pragma once
#include "component.hpp"
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

// Typed register plan and the byte-serial register bank that serves it.
//
// A bank answers a serial request of ten-bit words: a route word (the key's low nine bits), a
// command word {command, the key's top bits, 0, status}, the address's low and high words, and for
// a write four data words (byte 3 first, bit 9 its strobe). A write is not atomic: each byte lands
// in its own cycle, so a stalled request leaves the earlier bytes visible. An address no field
// names reads zero and ignores writes without an error.
namespace nocloom::detail {
enum class RegisterAccess {
  Constant, // reads value; ignores writes
  Control,  // reads back, written per byte strobe; resets to value
  Status,   // reads a hardware input; ignores writes
  Pulse,    // reads zero; a written one under its strobe pulses the output for one cycle
};
struct RegisterField {
  std::string name;
  unsigned word = 0; // 32-bit word offset in the bank
  unsigned lsb = 0;
  unsigned width = 1;
  RegisterAccess access = RegisterAccess::Control;
  std::uint64_t value = 0;
  // What a map says of it, and out of the identity's checksum.
  std::string unit = {}, description = {};
  bool operator==(const RegisterField &) const = default;
};
struct RegisterBankPlan {
  std::string name;
  unsigned route = 0; // 11-bit key of the bank in the register network
  std::vector<RegisterField> fields;
  // The bank's bytes, as a power of two: an address past them is not the bank's.
  unsigned address_bits = 8;
  bool operator==(const RegisterBankPlan &) const = default;
};
// The access's name in a map.
const char *register_access_name(RegisterAccess access);
// Rejects a malformed plan: bad names or geometry, overlapping bits, a value wider than its field.
Status check_register_bank(const RegisterBankPlan &plan);
// Field widths of a first-error record.
struct ErrorRecordShape {
  unsigned route_bits = 0, address_bits = 0, user_bits = 0, security_bits = 0;
  unsigned length_bits = 0, code_bits = 0;
  bool operator==(const ErrorRecordShape &) const = default;
};
// The register table of an error logger. Every offset of the logger is fixed here and nowhere else.
// A given revision replaces the generator version in the revision register.
RegisterBankPlan error_logger_bank(std::string name, unsigned route, const ErrorRecordShape &shape,
                                   std::optional<std::uint32_t> given_revision = std::nullopt);
// The bytes of an error logger's bank, as a power of two.
inline constexpr unsigned error_log_bank_address_bits = 8;
// The module nocloom_register_bank_<name>: faces request and response (valid, data, head, tail,
// and ready from the receiver), one port nl_field_<name> per control, status and pulse field.
// Bindings access.* name register operations and state names the parser state.
Result<NodeHardware> lower_register_bank(const RegisterBankPlan &plan);
} // namespace nocloom::detail
