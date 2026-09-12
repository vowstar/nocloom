#pragma once
#include "nocloom/definition.hpp"
#include <string>
#include <string_view>

namespace nocloom::detail {
// A Verilog-safe, nonreserved identifier of 1 to 128 characters.
bool valid_name(std::string_view name);
Diagnostic error(std::string code, std::string field, std::string message);
// Locates each diagnostic without a location at the nearest enclosing field of its configuration
// path that the source map knows.
std::vector<Diagnostic> locate(std::vector<Diagnostic> diagnostics, const SourceMap *sources);
} // namespace nocloom::detail
