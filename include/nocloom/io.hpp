#pragma once
#include "nocloom/generation.hpp"
#include <filesystem>

namespace nocloom::io {
struct WriteReport {
  std::vector<std::string> written_paths;
};
Result<ParsedDefinition> load_definition(const std::filesystem::path &path);
Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory);
} // namespace nocloom::io
