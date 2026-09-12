#pragma once
#include "component.hpp"
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

// Typed register plan and the byte-serial register bank that serves it.
//
// A bank answers a serial frame of nine-bit words, one data byte and its strobe. The header is
// one word: the bank's key in its chain above the register's index in the bank. A header alone
// asks for a read; four data words after it make a write (byte 3 first, bit 8 its strobe). A tail
// mark beside the last word ends the frame, and the word after it is a header. The bank returns
// the header as it came, a read's four bytes after it, a write's once the bytes have landed. A
// frame of another key passes to the next bank of the chain.
//
// A write is not atomic: each byte lands in its own cycle, so a stalled request leaves the
// earlier bytes visible. An address no field names reads zero and ignores writes without an error.
namespace nocloom::detail {
// The sizes of a frame. A data byte and its strobe make the word. A chain has at most 32 banks
// and a bank at most 16 registers, so a key and an index fill one word.
inline constexpr unsigned register_frame_word_bits = 9, register_key_bits = 5,
                          register_index_bits = 4;
enum class RegisterAccess {
  RoFixed, // reads value, fixed at generation, no flip-flop; ignores writes
  Rw,      // reads back, written per byte strobe; resets to value
  Ro,      // reads a hardware input; ignores writes
  Rw1c,    // reads a hardware input; a written one under its strobe pulses the clear output
};
struct RegisterField {
  std::string name;
  unsigned word = 0; // 32-bit word offset in the bank
  unsigned lsb = 0;
  unsigned width = 1;
  RegisterAccess access = RegisterAccess::Rw;
  std::uint64_t value = 0;
  // What a map says of it.
  std::string unit = {}, description = {};
  bool operator==(const RegisterField &) const = default;
};
struct RegisterBankPlan {
  std::string name;
  unsigned key = 0; // the bank's place in its chain
  std::vector<RegisterField> fields;
  // The bank's bytes, as a power of two: an address past them is not the bank's.
  unsigned address_bits = 6;
  bool operator==(const RegisterBankPlan &) const = default;
};
// The access's name in a map.
const char *register_access_name(RegisterAccess access);
// Rejects a malformed plan: bad names or geometry, overlapping bits, a value wider than its field.
Status check_register_bank(const RegisterBankPlan &plan);
// Field widths of a first-error record.
struct ErrorRecordShape {
  unsigned route_bits = 0, address_bits = 0, user_bits = 0, audit_bits = 0;
  unsigned length_bits = 0, class_bits = 0;
  // The route's parts a bank shows, lowest first; the direction stands above them.
  unsigned region_bits = 0, target_path_bits = 0, source_path_bits = 0, sequence_bits = 0;
  bool operator==(const ErrorRecordShape &) const = default;
};
// Where an error logger's bank stands and what its log has beside the record's shape.
struct ErrorLogPlace {
  unsigned log = 0, logger = 0; // the log in its window, the logger in its chain
  bool next_logger = false;     // the chain has a logger after this one
  bool target_user_flags = false;
  // Takes the revision register in place of the generator version.
  std::optional<std::uint32_t> given_revision = std::nullopt;
};
// Layout 2 of an error logger's register table: 0 and 1 are not layout numbers. Every offset of
// the logger is fixed here and nowhere else. A field the shape leaves out keeps its place; a
// narrower one keeps its low bits.
inline constexpr unsigned error_log_layout = 2;
inline constexpr unsigned error_log_state_word = 1;
RegisterBankPlan error_logger_bank(std::string name, const ErrorRecordShape &shape,
                                   const ErrorLogPlace &place = {});
// The fields of the layout at their full widths, for what describes the layout and not one bank.
std::vector<RegisterField> error_log_layout_fields();
// The bytes of an error logger's bank, as a power of two: eight fixed words and eight of user
// flags.
inline constexpr unsigned error_log_bank_address_bits = 6;
// The module nocloom_register_bank_<name>: faces request and response (valid, data, tail, and
// ready from the receiver), one port nl_field_<name> per Rw, Ro and Rw1c field, and
// nl_field_<name>_clear beside an Rw1c field.
// Bindings access.* name register operations and state names the parser state.
// A network window can collect a read on a shared 32-bit port. In that mode the serial
// response to a read consists of its header only.
Result<NodeHardware> lower_register_bank(const RegisterBankPlan &plan, bool shared_read = false);
} // namespace nocloom::detail
