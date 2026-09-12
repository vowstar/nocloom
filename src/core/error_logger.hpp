#pragma once
#include "component.hpp"
#include "register_bank.hpp"
#include <string>
#include <vector>

// Each target probe holds one failed packet's snapshot until delivery completes.
// Errors arriving while it is occupied are dropped. Logging never blocks data responses.
// Same-domain probes deliver whole records. Crossed probes send fixed-width words,
// lowest bits first, and a separate last flag through a backpressured FIFO.
// The snapshot is released only when the final word enters the FIFO.
// The receiver assembles the prefix and captures on the final word handshake.
// Word data, last and snapshot remain stable while a word waits.
// Reset discards partial frames.
//
// Each logger keeps its first record until clear. A full slot drops later records,
// or waits when hold is enabled. Clear wins over capture but fields still update.
// Fault follows valid and alarm enable one logger clock later.
// Filters select fields and error codes. A chain forwards rejected records,
// or every record with pass_always. Chain hold affects only logging.
namespace nocloom::detail {
// A bit of a record's field and the value a filter wants of it.
struct RecordFilterBit {
  unsigned bit = 0;
  bool value = false;
  bool operator==(const RecordFilterBit &) const = default;
};
// A further logger of a chain: what ErrorLoggerPlan says of its first.
struct ChainedLogger {
  std::vector<unsigned> ignored_codes;
  std::vector<RecordFilterBit> user_filter, security_filter;
  bool pass_always = false;
  bool operator==(const ChainedLogger &) const = default;
};
struct RecordLayout {
  struct Field {
    std::string name;
    unsigned width = 0, lsb = 0;
    bool operator==(const Field &) const = default;
  };
  std::vector<Field> fields;
  unsigned bits = 0;
  unsigned words(unsigned word_bits) const { return bits / word_bits + (bits % word_bits != 0); }
  bool operator==(const RecordLayout &) const = default;
};
struct ErrorLoggerPlan {
  std::string name;
  ErrorRecordShape shape;
  unsigned targets = 1;      // observed targets
  unsigned serial_words = 1; // serial words of a record
  unsigned serial_word_bits = 32;
  // Error codes the logger drops as it takes them (they wait all the same).
  std::vector<unsigned> ignored_codes;
  // Flag values a record must have or the logger drops it as it takes it (it stalls all the same).
  std::vector<RecordFilterBit> user_filter, security_filter;
  // The record leaves to the next logger though this one logs it.
  bool pass_always = false;
  // The loggers after this one, each with its bank's faces and its alarm.
  std::vector<ChainedLogger> chain;
  // Every packet's length field holds this one value (a network of APB words); zero: it varies.
  unsigned fixed_length = 0;
  // The targets are in another clock domain: faces word<k> in place of event<k>.
  bool crossed = false;
  bool operator==(const ErrorLoggerPlan &) const = default;
};
RecordLayout error_record_layout(const ErrorLoggerPlan &plan);
// Same-domain inputs event<k> carry moved, head, tail, failed and record fields.
// Crossed inputs word<k> carry valid, data and last with ready backpressure.
// Controls provide alarm_enable, serial_hold_when_full and record_clear_valid.
// Outputs provide record fields and alarm. Chain slots use numeric suffixes.
Result<NodeHardware> lower_error_logger(const ErrorLoggerPlan &plan);
inline unsigned error_record_word_bits(const ErrorLoggerPlan &plan) {
  return plan.serial_word_bits;
}
// Sender: event_* and word_ready inputs, word_valid, word_data and word_last outputs.
Result<NodeHardware> lower_error_sender(const ErrorLoggerPlan &plan, unsigned target);
// A target error code. An absent code reads zero.
enum ErrorCode : unsigned { CodeTarget = 1, CodeDecode = 2 };
} // namespace nocloom::detail
