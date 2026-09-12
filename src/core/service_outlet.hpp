#pragma once
#include "component.hpp"
#include "exclusive.hpp"
#include <cstdint>
#include <nocloom/definition.hpp>
#include <vector>

// The outlet of a service target: a target with no ports of its own that serves one register bank
// (register_bank.hpp) over a byte-serial path. One packet at a time, and only single-beat words of
// four bytes reach it.
//
// A word goes out as a route word, a command word, two address words and, for a write inside the
// bank, four data words, the first in the cycle the word arrives. A read leaves the face at its
// last address word, a write at its last data word. An address outside the bank goes out with an
// error status and a zero address and no data words; the bank passes it through, and that is the
// answer. A decode miss never reaches the bank and answers in the next cycle.
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
  unsigned window_bits = 20, bank_bits = 8;
  unsigned route = 0; // the bank's key
  // Several banks: where each sits and its branch of the service switch. The window's bank bits
  // choose; an address of no bank goes out on branch 0.
  struct Place {
    std::uint64_t base = 0;
    unsigned route = 0, branch = 0;
  };
  std::vector<Place> places{};
};
// The module nocloom_service_outlet_<name>: faces wreq, rreq, wrsp and rrsp, and the bank's serial
// port on hints bank_valid, bank_data, bank_head, bank_tail, bank_ready and bank_out_valid,
// bank_out_data, bank_out_tail, bank_out_ready; with several banks also bank_branch.
Result<NodeHardware> lower_service_outlet(const ServiceOutletProfile &profile);
// The service switch in front of several branches of banks: one serial word register, holding the
// word and the branch it goes to, and the branches' answers merged. One packet is on the path at a
// time, so an answer has no one to wait for.
//
// The module nocloom_service_switch_<name>: request hints in_valid, in_data, in_head, in_tail,
// in_branch and in_ready; per branch k out<k>_valid, out<k>_data, out<k>_head, out<k>_tail,
// out<k>_ready and back<k>_valid, back<k>_data, back<k>_tail; answer hints back_valid, back_data,
// back_tail.
Result<NodeHardware> lower_service_switch(const std::string &name, unsigned branches);
} // namespace nocloom::detail
