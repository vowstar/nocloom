#pragma once
#include "component.hpp"
#include "exclusive.hpp"
#include "register_bank.hpp"
#include <cstdint>
#include <nocloom/definition.hpp>
#include <vector>

// The outlet of a service target: a target with no ports of its own that serves register banks
// (register_bank.hpp) over a byte-serial path. One packet at a time, and only single-beat words of
// four bytes reach it.
//
// The window's offset bits choose the bank and never leave the outlet. A word for a bank goes out
// as a frame: its header in the cycle the word arrives, and for a write four data words. A read
// leaves the face at its header, a write at its last data word. A word for no bank of the window
// and a decode miss send no frame and answer in the next cycle.
namespace nocloom::detail {
struct ServiceOutletProfile {
  std::string name;
  unsigned id_bits = 0, len_bits = 8, source_bits = 0, tag_bits = 0;
  unsigned user_bits = 0, response_user_bits = 0;
  bool exclusive = false;
  unsigned transport_address_bits = 32;
  AddressBlock arrival{};
  // Where a run ends (message.hpp point 18); zero without runs.
  unsigned response_run_bits = 0;
  // Offset bits of the target's window (at most 20) and of the bank in it.
  unsigned window_bits = 20, bank_bits = error_log_bank_address_bits;
  unsigned key = 0; // the bank's key
  // Several banks: where each sits, its key and its branch of the service switch. The window's
  // bank bits choose.
  struct Place {
    std::uint64_t base = 0;
    unsigned key = 0, branch = 0;
  };
  std::vector<Place> places{};
};
// The module nocloom_service_outlet_<name>: faces wreq, rreq, wrsp and rrsp, and the bank's serial
// port on hints bank_valid, bank_data, bank_tail, bank_ready and bank_out_valid,
// bank_out_data, bank_out_tail, bank_out_ready; with several banks also bank_branch.
Result<NodeHardware> lower_service_outlet(const ServiceOutletProfile &profile,
                                          bool shared_read = false);
// Turn the sampled word into a serial response before a clock crossing. The branch submits
// exactly one completed access before the next request is admitted.
Result<NodeHardware> lower_service_read_frame();
// The service switch in front of several branches of banks: one serial word register, holding the
// word and the branch it goes to, and the branches' answers merged. One packet is on the path at a
// time, so an answer has no one to wait for.
//
// The module nocloom_service_switch_<name>: request hints in_valid, in_data, in_tail, in_branch
// and in_ready; per branch k out<k>_valid, out<k>_data, out<k>_tail, out<k>_ready and
// back<k>_valid, back<k>_data, back<k>_tail; answer hints back_valid, back_data, back_tail.
Result<NodeHardware> lower_service_switch(const std::string &name, unsigned branches);
// A register stage between two banks of a chain: a frame word and its tail mark leave it a cycle
// after they enter, and no ready passes it. Its two places keep every word in order while the next
// bank stalls.
//
// The module nocloom_service_stage: hints in_valid, in_data, in_tail, in_ready and out_valid,
// out_data, out_tail, out_ready.
Result<NodeHardware> lower_service_stage();
} // namespace nocloom::detail
