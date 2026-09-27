#pragma once
#include "async_fifo.hpp"
#include "network_plan.hpp"

namespace nocloom::detail {
struct RelatedAdapter {
  HardwareIR ir;
  std::array<FifoDomain, 2> domains;
  unsigned depth = 0, count_bits = 0, index_bits = 0;
  bool fast = false, slow_pipeline = false, slow_from_sender = false, reset_all = false;
  ValueId slow_enable{};
  ValueId valid{}, ready{}, write{}, full{};
  ValueId write_index{}, write_count{}, read_index{}, read_count{};
  std::vector<ValueId> read_count_sync, write_count_sync;
  ValueId out_ready{}, out_valid{}, output_valid{}, nonempty{}, read{};
  ValueId admission_open{};
  std::vector<FifoField> fields;
  std::vector<std::pair<std::string, unsigned>> layout;
  std::vector<ValueId> pipeline_payload, pipeline_control;
  ValueId returned_read_count() const {
    return read_count_sync.empty() ? read_count : read_count_sync.back();
  }
  ValueId returned_write_count() const {
    return write_count_sync.empty() ? write_count : write_count_sync.back();
  }
};

Result<RelatedAdapter>
lower_related_sync(const DomainPlan &from, const DomainPlan &to, unsigned depth, bool fast,
                   const std::vector<std::pair<std::string, unsigned>> &fields,
                   bool reset_all = false);
Result<RelatedAdapter>
lower_edge_aligned(const DomainPlan &from, const DomainPlan &to, bool slow_from_sender,
                   bool slow_pipeline, const std::vector<std::pair<std::string, unsigned>> &fields,
                   bool reset_all = false);
Status verify_related_adapter(const RelatedAdapter &adapter, bool edge_aligned);
} // namespace nocloom::detail
