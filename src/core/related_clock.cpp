#include "related_clock.hpp"
#include "diagnostic.hpp"
#include "link_pipeline.hpp"
#include <bit>
#include <set>

namespace nocloom::detail {
namespace {
ValueId both(Builder &b, ValueId a, ValueId c) { return b.binary(Op::BitAnd, a, c); }
ValueId either(Builder &b, ValueId a, ValueId c) { return b.binary(Op::BitOr, a, c); }
ValueId negate(Builder &b, ValueId a) { return b.unary(Op::BitNot, a, 1); }
ValueId increment(Builder &b, ValueId value) {
  const auto width = b.ir.values.at(value.index).width;
  const auto mask = width >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << width) - 1;
  return b.binary(Op::Subtract, value, b.constant(width, mask));
}
ValueId wrap_increment(Builder &b, ValueId value, unsigned depth) {
  const auto width = b.ir.values.at(value.index).width;
  return b.select(b.binary(Op::Equal, value, b.constant(width, depth - 1)), b.constant(width, 0),
                  increment(b, value));
}
ValueId exclusive(Builder &b, ValueId a, ValueId c) {
  const auto width = b.ir.values.at(a.index).width;
  return b.binary(Op::BitAnd, either(b, a, c), b.unary(Op::BitNot, both(b, a, c), width));
}
ValueId to_gray(Builder &b, ValueId value) {
  const auto width = b.ir.values.at(value.index).width;
  return exclusive(b, value, b.concat(b.constant(1, 0), b.slice(value, 1, width - 1)));
}
ValueId from_gray(Builder &b, ValueId code) {
  const auto width = b.ir.values.at(code.index).width;
  auto bit = b.slice(code, width - 1, 1);
  auto result = bit;
  for (unsigned i = width - 1; i-- > 0;) {
    bit = exclusive(b, bit, b.slice(code, i, 1));
    result = b.concat(result, bit);
  }
  return result;
}
ValueId gray_increment(Builder &b, ValueId code) {
  return to_gray(b, increment(b, from_gray(b, code)));
}
bool fields_ok(const DomainPlan &from, const DomainPlan &to,
               const std::vector<std::pair<std::string, unsigned>> &fields) {
  if (!valid_name(from.name) || !valid_name(to.name) || from.name == to.name || fields.empty() ||
      fields.size() > 128)
    return false;
  std::set<std::string> field_names;
  for (const auto &[name, width] : fields)
    if (!valid_name(name) || !field_names.insert(name).second || width == 0 || width > 1024)
      return false;
  return true;
}
std::array<FifoDomain, 2> domain_ports(Builder &b, const DomainPlan &from, const DomainPlan &to) {
  std::array<FifoDomain, 2> domains;
  for (unsigned d = 0; d < 2; ++d) {
    auto &domain = domains[d];
    domain.plan = d == 0 ? from : to;
    domain.clock = b.input("nl_clk_" + decimal(d), 1, "domain:" + domain.plan.name, "clock");
    domain.reset = b.input("nl_reset_n_" + decimal(d), 1, "domain:" + domain.plan.name, "reset_n");
  }
  return domains;
}
ValueId stream_input(Builder &b, const std::string &name, unsigned width) {
  return b.input("nl_stream_" + name, width, "stream", name);
}
void stream_outputs(Builder &b, const RelatedAdapter &f) {
  b.output("nl_stream_ready", f.ready, "stream", "ready");
  b.output("nl_stream_out_valid", f.output_valid, "stream", "out_valid");
  for (std::size_t i = 0; i < f.fields.size(); ++i)
    b.output("nl_stream_out_f" + decimal(i), f.fields[i].output, "stream", "out_f" + decimal(i));
}
Result<RelatedAdapter> config_error() {
  return Result<RelatedAdapter>::failure(
      error("NCL_RELATED_CLOCK_CONFIG", "hardware.related_clock", "Invalid related-clock adapter"));
}
} // namespace

Result<RelatedAdapter>
lower_related_sync(const DomainPlan &from, const DomainPlan &to, unsigned depth, bool fast,
                   const std::vector<std::pair<std::string, unsigned>> &fields, bool reset_all) {
  if (!fields_ok(from, to, fields) || depth < 2 || depth > 4 || (fast && depth != 2))
    return config_error();
  Builder b;
  b.ir.name = "nocloom_related_sync";
  RelatedAdapter f{};
  f.depth = depth;
  f.fast = fast;
  f.reset_all = reset_all;
  f.count_bits = async_fifo_count_bits(depth);
  f.index_bits = async_fifo_index_bits(depth);
  f.layout = fields;
  f.domains = domain_ports(b, from, to);
  const auto &a = f.domains[0], &z = f.domains[1];
  f.valid = stream_input(b, "valid", 1);
  f.out_ready = stream_input(b, "out_ready", 1);
  auto payload = [&](const FifoDomain &d, unsigned width) {
    return reset_all ? b.state(width, d.clock, d.reset) : b.held_state(width, d.clock);
  };
  f.write_index = b.state(f.index_bits, a.clock, a.reset);
  f.write_count = b.state(f.count_bits, a.clock, a.reset);
  f.read_index = b.state(f.index_bits, z.clock, z.reset);
  f.read_count = b.state(f.count_bits, z.clock, z.reset);
  if (!fast) {
    f.read_count_sync.push_back(b.state(f.count_bits, a.clock, a.reset));
    f.write_count_sync.push_back(b.state(f.count_bits, z.clock, z.reset));
    b.update(f.read_count_sync.back(), f.read_count);
    b.update(f.write_count_sync.back(), f.write_count);
  }
  f.full = b.binary(
      Op::Equal,
      b.binary(Op::Subtract, from_gray(b, f.write_count), from_gray(b, f.returned_read_count())),
      b.constant(f.count_bits, depth));
  f.ready = negate(b, f.full);
  f.write = both(b, f.valid, f.ready);
  b.update(f.write_index,
           b.select(f.write, wrap_increment(b, f.write_index, depth), f.write_index));
  b.update(f.write_count, b.select(f.write, gray_increment(b, f.write_count), f.write_count));
  f.nonempty = negate(b, b.binary(Op::Equal, f.read_count, f.returned_write_count()));
  f.out_valid = f.nonempty;
  f.output_valid = f.nonempty;
  f.read = both(b, f.nonempty, f.out_ready);
  b.update(f.read_index, b.select(f.read, wrap_increment(b, f.read_index, depth), f.read_index));
  b.update(f.read_count, b.select(f.read, gray_increment(b, f.read_count), f.read_count));
  std::vector<ValueId> slot_write, slot_read;
  for (unsigned i = 0; i < depth; ++i) {
    slot_write.push_back(
        both(b, f.write, b.binary(Op::Equal, f.write_index, b.constant(f.index_bits, i))));
    slot_read.push_back(b.binary(Op::Equal, f.read_index, b.constant(f.index_bits, i)));
  }
  for (std::size_t i = 0; i < fields.size(); ++i) {
    FifoField field{};
    field.name = fields[i].first;
    field.width = fields[i].second;
    field.input = stream_input(b, "in_f" + decimal(i), field.width);
    for (unsigned s = 0; s < depth; ++s) {
      auto slot = payload(a, field.width);
      b.update(slot, b.select(slot_write[s], field.input, slot));
      field.storage.push_back(slot);
    }
    field.selected = field.storage.back();
    for (unsigned s = depth - 1; s-- > 0;)
      field.selected = b.select(slot_read[s], field.storage[s], field.selected);
    field.received = field.selected;
    field.output = b.select(f.output_valid, field.received, b.constant(field.width, 0));
    f.fields.push_back(std::move(field));
  }
  stream_outputs(b, f);
  f.ir = std::move(b.ir);
  if (auto status = verify_related_adapter(f, false); !status)
    return Result<RelatedAdapter>::failure(status.diagnostics());
  return Result<RelatedAdapter>::success(std::move(f));
}

Result<RelatedAdapter>
lower_edge_aligned(const DomainPlan &from, const DomainPlan &to, bool slow_from_sender,
                   bool slow_pipeline, const std::vector<std::pair<std::string, unsigned>> &fields,
                   bool reset_all) {
  if (!fields_ok(from, to, fields))
    return config_error();
  Builder b;
  b.ir.name = "nocloom_edge_aligned";
  RelatedAdapter f{};
  f.slow_pipeline = slow_pipeline;
  f.slow_from_sender = slow_from_sender;
  f.reset_all = reset_all;
  f.layout = fields;
  f.domains = domain_ports(b, from, to);
  const auto &a = f.domains[0], &z = f.domains[1];
  f.slow_enable = b.input("nl_slow_enable", 1, "slow", "enable");
  f.valid = stream_input(b, "valid", 1);
  f.out_ready = stream_input(b, "out_ready", 1);
  std::vector<ValueId> inputs;
  for (std::size_t i = 0; i < fields.size(); ++i)
    inputs.push_back(stream_input(b, "in_f" + decimal(i), fields[i].second));
  auto base_valid = f.valid;
  auto base_data = inputs;
  ValueId base_ready;
  const bool needs_admission = slow_from_sender || !slow_pipeline;
  if (needs_admission) {
    const auto &owner = slow_from_sender ? z : a;
    f.admission_open = b.state(1, owner.clock, owner.reset, 1);
  }
  if (slow_from_sender) {
    base_ready = b.select(f.admission_open, f.out_ready, b.constant(1, 1));
    if (slow_pipeline) {
      auto pipe = lower_link_pipeline(b, a.clock, a.reset, f.valid, inputs, base_ready,
                                      LinkPipeline{true, true}, reset_all);
      f.ready = pipe.ready;
      base_valid = pipe.valid;
      base_data = pipe.data;
      f.pipeline_control = pipe.control;
      f.pipeline_payload = pipe.payload;
    } else {
      f.ready = base_ready;
    }
  } else if (slow_pipeline) {
    auto pipe = lower_link_pipeline(b, z.clock, z.reset, base_valid, base_data, f.out_ready,
                                    LinkPipeline{true, true}, reset_all);
    base_ready = both(b, pipe.ready, f.slow_enable);
    f.ready = base_ready;
    f.output_valid = pipe.valid;
    base_data = pipe.data;
    f.pipeline_control = pipe.control;
    f.pipeline_payload = pipe.payload;
  } else {
    base_ready = both(b, both(b, f.out_ready, f.admission_open), f.slow_enable);
    f.ready = base_ready;
    base_valid = both(b, base_valid, f.admission_open);
  }
  if (needs_admission) {
    if (slow_from_sender)
      base_valid = both(b, base_valid, f.admission_open);
    f.output_valid = base_valid;
    const auto close = slow_from_sender ? both(b, base_valid, f.out_ready) : negate(b, f.valid);
    const auto after_event = b.select(close, b.constant(1, 0), f.admission_open);
    b.update(f.admission_open, b.select(f.slow_enable, b.constant(1, 1), after_event));
  }
  f.out_valid = base_valid;
  f.nonempty = base_valid;
  f.write = both(b, f.valid, f.ready);
  f.read = both(b, f.output_valid, f.out_ready);
  for (std::size_t i = 0; i < fields.size(); ++i) {
    FifoField field{};
    field.name = fields[i].first;
    field.width = fields[i].second;
    field.input = inputs[i];
    field.selected = base_data[i];
    field.received = base_data[i];
    field.output = b.select(f.output_valid, base_data[i], b.constant(field.width, 0));
    f.fields.push_back(std::move(field));
  }
  stream_outputs(b, f);
  f.ir = std::move(b.ir);
  if (auto status = verify_related_adapter(f, true); !status)
    return Result<RelatedAdapter>::failure(status.diagnostics());
  return Result<RelatedAdapter>::success(std::move(f));
}

Status verify_related_adapter(const RelatedAdapter &f, bool edge_aligned) {
  if (auto status = verify_structure(f.ir); !status)
    return status;
  if (!fields_ok(f.domains[0].plan, f.domains[1].plan, f.layout) ||
      f.fields.size() != f.layout.size() || !f.ir.synchronizer_stages.empty())
    return Status::failure(error("NCL_INTERNAL_RELATED_CLOCK", "hardware.related_clock",
                                 "Invalid related-clock layout or synchronizer declaration"));
  if (!edge_aligned && (f.depth < 2 || f.depth > 4 || (f.fast && f.depth != 2) ||
                        f.read_count_sync.size() != (f.fast ? 0U : 1U) ||
                        f.write_count_sync.size() != (f.fast ? 0U : 1U)))
    return Status::failure(
        error("NCL_INTERNAL_RELATED_CLOCK", "hardware.related_clock", "Invalid Sync shape"));
  if (edge_aligned &&
      (f.depth != 0 || (f.admission_open.index != 0) != (f.slow_from_sender || !f.slow_pipeline)))
    return Status::failure(
        error("NCL_INTERNAL_RELATED_CLOCK", "hardware.related_clock", "Invalid EdgeAligned shape"));
  return Status::success({});
}
} // namespace nocloom::detail
