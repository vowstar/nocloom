#pragma once
#include "nocloom/generation.hpp"
#include <nlohmann/json_fwd.hpp>

namespace nocloom::detail {
struct OutputFiles {
  nlohmann::json &bundle;
  ArtifactSet &artifacts;
  void add(std::string path, std::string_view role, std::string text);
};

Status check_backend(const GenerationOptions &options, const SourceMap *sources);
Status process_optional_outputs(const Definition &definition, const GenerationOptions &options,
                                const nlohmann::json &map, OutputFiles *output = nullptr);
Status process_ipxact(const Definition &definition, const nlohmann::json &map, OutputFiles *output);
} // namespace nocloom::detail
