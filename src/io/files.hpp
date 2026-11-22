#pragma once
#include "nocloom/io.hpp"
#include <functional>
#include <optional>
#include <string>
#include <utility>

namespace nocloom::io::detail {
// The first two artifact paths, in artifact order, that differ only in ASCII letter case.
std::optional<std::pair<std::string, std::string>> letter_case_twins(const ArtifactSet &artifacts);
// Whether the parent of an output directory finds an entry under a name that differs from the
// entry's name only in ASCII letter case. No value when no entry can show it.
using LetterCaseProbe = std::function<std::optional<bool>(const std::filesystem::path &)>;
// The probe of write_artifacts: it reads the parent and creates nothing.
std::optional<bool> ignores_letter_case(const std::filesystem::path &directory);
// write_artifacts with the given probe of the output directory.
Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory,
                                    const LetterCaseProbe &probe);
// Letter case twins take their names from a bound cell module or from the network name: an
// NCL_IO_CASE diagnostic gets that field and its place in the definition.
std::vector<Diagnostic> located(std::vector<Diagnostic> diagnostics, const ArtifactSet &artifacts,
                                const ParsedDefinition &source);
} // namespace nocloom::io::detail
