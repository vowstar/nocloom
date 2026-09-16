#pragma once
#include "hardware.hpp"
#include <algorithm>
#include <functional>
#include <map>
#include <set>
#include <vector>

namespace nocloom::detail {
// Visits every value of root's cone once, depth first: states go to on_state,
// inputs to on_input, constants are leaves; other values queue their operands
// in operand index order using operand_count. The stack visits the last queued
// operand first.
// A value `stop` accepts ends its branch: it and its cone are not visited.
template <typename S, typename I, typename P>
void walk_cone(const HardwareIR &ir, ValueId root, S on_state, I on_input, P stop) {
  std::set<std::size_t> seen;
  std::vector<ValueId> todo{root};
  while (!todo.empty()) {
    const auto id = todo.back();
    todo.pop_back();
    if (!seen.insert(id.index).second || stop(id))
      continue;
    const auto &v = ir.values[id.index];
    if (v.op == Op::State)
      on_state(id);
    else if (v.op == Op::Input)
      on_input(id);
    else if (v.op != Op::Constant)
      for (unsigned i = 0; i < operand_count(v.op); ++i)
        todo.push_back(v.operands[i]);
  }
}
template <typename S, typename I>
void walk_cone(const HardwareIR &ir, ValueId root, S on_state, I on_input) {
  walk_cone(ir, root, on_state, on_input, [](ValueId) { return false; });
}
// Depth-first visit of a named digraph: roots in range order, successors in
// container order; finished nodes append to ordered. A back edge fails the
// visit with cycle = the stack slice from the repeated node, plus that node.
template <typename Roots, typename Next>
bool topo_visit(const Roots &roots, const Next &successors, std::vector<std::string> &ordered,
                std::vector<std::string> &cycle) {
  std::map<std::string, unsigned> colors;
  std::vector<std::string> stack;
  const std::function<bool(const std::string &)> visit = [&](const std::string &name) {
    colors[name] = 1;
    stack.push_back(name);
    for (const auto &next : successors(name)) {
      if (colors[next] == 1) {
        cycle.assign(std::find(stack.begin(), stack.end(), next), stack.end());
        cycle.push_back(next);
        return false;
      }
      if (colors[next] == 0 && !visit(next))
        return false;
    }
    stack.pop_back();
    colors[name] = 2;
    ordered.push_back(name);
    return true;
  };
  return !std::ranges::any_of(roots,
                              [&](const auto &root) { return colors[root] == 0 && !visit(root); });
}
} // namespace nocloom::detail
