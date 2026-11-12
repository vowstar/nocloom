#include "nocloom/generation.hpp"
#include "backends/c_header.hpp"
#include "backends/constraints/cdc_sdc.hpp"
#include "core/axi4_output.hpp"
#include "core/diagnostic.hpp"

namespace nocloom {
std::string_view version() noexcept { return NOCLOOM_VERSION; }

Result<ArtifactSet> emit(const Plan &plan, const GenerationOptions &options) {
  if (options.backend != Backend::Verilog2001)
    return Result<ArtifactSet>::failure(detail::error("NCL_UNSUPPORTED_BACKEND", "output.backend",
                                                      "Only Verilog-2001 is supported"));
  if (!plan.data_)
    return Result<ArtifactSet>::failure(
        detail::error("NCL_INTERNAL_PLAN", "plan", "Plan has been moved from"));
  auto artifacts = detail::emit_axi4_network(*plan.data_, options.register_reset);
  if (artifacts && options.cdc_constraints)
    artifacts = detail::add_cdc_constraints(std::move(artifacts).value());
  if (artifacts && options.c_header)
    artifacts = detail::add_c_headers(std::move(artifacts).value());
  return artifacts;
}

Result<ArtifactSet> generate(const Definition &definition, const GenerationOptions &options,
                             const SourceMap *sources) {
  auto plan = make_plan(definition, sources);
  if (!plan)
    return Result<ArtifactSet>::failure(plan.diagnostics());
  return emit(plan.value(), options);
}
} // namespace nocloom
