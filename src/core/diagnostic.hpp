#pragma once
#include "nocloom/definition.hpp"
#include <string>
#include <string_view>

namespace nocloom::detail {
// A reserved keyword of Verilog-2001.
bool keyword(std::string_view name);
// A Verilog-2001 identifier of 1 to 128 characters without the nl_ or nocloom_ prefix.
bool valid_name(std::string_view name);
// A name the generator derives from names of the definition, such as the bank <log>_<n> or the
// module nocloom_axi4_upsize_<target> of a component: letters, digits and underscores from a
// letter, no keyword, and at most 1024 characters, the identifier length that every tool takes
// (IEEE 1364-2005 3.7, IEEE 1800-2017 5.6).
bool derived_name(std::string_view name);
Diagnostic error(std::string code, std::string field, std::string message);
// Locates each diagnostic without a location at the nearest enclosing field of its configuration
// path that the source map knows.
std::vector<Diagnostic> locate(std::vector<Diagnostic> diagnostics, const SourceMap *sources);
} // namespace nocloom::detail
