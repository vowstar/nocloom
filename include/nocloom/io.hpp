#pragma once
#include "nocloom/generation.hpp"
#include <filesystem>

namespace nocloom::io {
struct WriteReport {
  std::vector<std::string> written_paths;
};
Result<ParsedDefinition> load_definition(const std::filesystem::path &path);
// Writes the artifacts into a new directory and replaces no file. Artifacts whose paths differ
// only in ASCII letter case need a directory that keeps them apart: the parent of the new
// directory is tested first, and NCL_IO_CASE reports a directory that holds such names as one file,
// before any file is written, or at the twin when the test cannot tell.
Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory);
// The same for the artifacts of a parsed definition: NCL_IO_CASE then has the field that gives
// the files their names and its place in the definition.
Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory,
                                    const ParsedDefinition &source);
} // namespace nocloom::io
