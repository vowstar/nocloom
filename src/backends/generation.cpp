#include "nocloom/generation.hpp"
#include "backends/address_table.hpp"
#include "backends/c_header.hpp"
#include "backends/constraints/cdc_sdc.hpp"
#include "backends/optional_outputs.hpp"
#include "core/axi4_output.hpp"
#include "core/diagnostic.hpp"
#include <algorithm>
#include <nlohmann/json.hpp>

namespace nocloom {
std::string_view version() noexcept { return NOCLOOM_VERSION; }

namespace detail {
void OutputFiles::add(std::string path, std::string_view role, std::string text) {
  bundle.at("files").push_back({{"path", path}, {"role", role}});
  artifacts.push_back({std::move(path), std::move(text)});
}

Status check_backend(const GenerationOptions &options, const SourceMap *sources) {
  if (options.backend != Backend::Verilog2001)
    return Status::failure(locate(
        {error("NCL_UNSUPPORTED_BACKEND", "output.backend", "Only Verilog-2001 is supported")},
        sources));
  return Status::success({});
}

Status process_optional_outputs(const Definition &definition, const GenerationOptions &options,
                                const nlohmann::json &map, OutputFiles *output) {
  for (const auto &[requested, process] :
       {std::pair{options.cdc_constraints, &process_cdc_constraints},
        std::pair{options.c_header, &process_c_headers},
        std::pair{options.address_table, &process_address_table}})
    if (requested)
      if (auto status = process(map, output); !status)
        return status;
  if (options.ipxact)
    return process_ipxact(definition, map, output);
  return Status::success({});
}
} // namespace detail

Result<ArtifactSet> emit(const Plan &plan, const GenerationOptions &options) {
  if (auto status = detail::check_backend(options, nullptr); !status)
    return Result<ArtifactSet>::failure(status.diagnostics());
  if (!plan.data_)
    return Result<ArtifactSet>::failure(
        detail::error("NCL_INTERNAL_PLAN", "plan", "Plan has been moved from"));
  const bool extra =
      options.cdc_constraints || options.c_header || options.address_table || options.ipxact;
  nlohmann::json map;
  auto emitted =
      detail::emit_axi4_network(*plan.data_, options.register_reset, extra ? &map : nullptr);
  if (!emitted || !extra)
    return emitted;
  auto &artifacts = emitted.value();
  const auto bundle_path = map.at("module").get<std::string>() + ".bundle.json";
  auto bundle =
      nlohmann::json::parse(std::ranges::find(artifacts, bundle_path, &Artifact::path)->text);
  detail::OutputFiles output{bundle, artifacts};
  auto status = detail::process_optional_outputs(*plan.definition_, options, map, &output);
  if (!status)
    return Result<ArtifactSet>::failure(status.diagnostics());
  std::ranges::find(artifacts, bundle_path, &Artifact::path)->text = bundle.dump(2) + "\n";
  std::ranges::sort(artifacts, {}, &Artifact::path);
  return emitted;
}

Result<ArtifactSet> generate(const Definition &definition, const GenerationOptions &options,
                             const SourceMap *sources) {
  if (auto status = detail::check_backend(options, sources); !status)
    return Result<ArtifactSet>::failure(status.diagnostics());
  auto plan = make_plan(definition, sources, options.validation);
  if (!plan)
    return Result<ArtifactSet>::failure(plan.diagnostics());
  return emit(plan.value(), options);
}
} // namespace nocloom
