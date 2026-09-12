#include "diagnostic.hpp"
#include <algorithm>
#include <bit>
#include <set>
#include <tuple>

namespace nocloom::detail {
Diagnostic error(std::string code, std::string field, std::string message) {
  return {std::move(code), std::move(field), std::move(message), {}, {}};
}
std::vector<Diagnostic> locate(std::vector<Diagnostic> diagnostics, const SourceMap *sources) {
  for (auto &d : diagnostics)
    for (auto key = d.field; sources && !d.location && !key.empty();) {
      if (auto it = sources->find(key); it != sources->end())
        d.location = it->second;
      const auto pos = key.find_last_of(".[");
      key = pos == std::string::npos ? "" : key.substr(0, pos);
    }
  return diagnostics;
}

bool valid_name(std::string_view name) {
  const auto letter = [](char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); };
  if (name.empty() || name.size() > 128 || !letter(name.front()) || name.starts_with("nl_") ||
      name.starts_with("nocloom_"))
    return false;
  for (char c : name)
    if (!letter(c) && !(c >= '0' && c <= '9') && c != '_')
      return false;
  // The Verilog-2001 keywords.
  static const auto keywords = [] {
    std::set<std::string_view> set;
    constexpr std::string_view all =
        "always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos "
        "config deassign default defparam design disable edge else end endcase endconfig "
        "endfunction endgenerate endmodule endprimitive endspecify endtable endtask event "
        "for force forever fork function generate genvar highz0 highz1 if ifnone incdir "
        "include initial inout input instance integer join large liblist library "
        "localparam macromodule medium module nand negedge nmos nor noshowcancelled not "
        "notif0 notif1 or output parameter pmos posedge primitive pull0 pull1 pulldown "
        "pullup pulsestyle_onevent pulsestyle_ondetect rcmos real realtime reg release "
        "repeat rnmos rpmos rtran rtranif0 rtranif1 scalared showcancelled signed small "
        "specify specparam strong0 strong1 supply0 supply1 table task time tran tranif0 "
        "tranif1 tri tri0 tri1 triand trior trireg unsigned use vectored wait wand weak0 "
        "weak1 while wire wor xnor xor";
    for (std::size_t at = 0; at < all.size();) {
      const auto end = std::min(all.find(' ', at), all.size());
      set.insert(all.substr(at, end - at));
      at = end + 1;
    }
    return set;
  }();
  return !keywords.contains(name);
}
} // namespace nocloom::detail
