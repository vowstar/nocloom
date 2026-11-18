#pragma once

#include "nocloom/definition.hpp"
#include <memory>
#include <string_view>

namespace nocloom {
struct Artifact {
  std::string path;
  std::string text;
  bool operator==(const Artifact &) const = default;
};
using ArtifactSet = std::vector<Artifact>;
namespace detail {
struct NetworkPlan;
}

// An immutable snapshot. Creating a plan validates and normalizes the complete definition.
class Plan {
public:
  Plan(const Plan &) = default;
  Plan(Plan &&) noexcept = default;
  Plan &operator=(const Plan &) = default;
  Plan &operator=(Plan &&) noexcept = default;

private:
  Plan(std::shared_ptr<const detail::NetworkPlan> data, const Definition &definition)
      : data_(std::move(data)), definition_(std::make_shared<const Definition>(definition)) {}
  std::shared_ptr<const detail::NetworkPlan> data_;
  std::shared_ptr<const Definition> definition_;
  friend Result<Plan> make_plan(const Definition &, const SourceMap *, const ValidationOptions &);
  friend Result<ArtifactSet> emit(const Plan &, const GenerationOptions &);
  friend Status validate(const Definition &, const GenerationOptions &, const SourceMap *);
};

std::string_view version() noexcept;
// Source locations affect diagnostics only. They never enter generated artifacts.
Result<Plan> make_plan(const Definition &definition, const SourceMap *sources = nullptr,
                       const ValidationOptions &validation = {});
// Checks the definition, composed hardware, map and requested outputs without returning artifacts.
Status validate(const Definition &definition, const GenerationOptions &options = {},
                const SourceMap *sources = nullptr);
// These functions perform no file I/O and have no console side effects.
Result<ArtifactSet> emit(const Plan &plan, const GenerationOptions &options = {});
Result<ArtifactSet> generate(const Definition &definition, const GenerationOptions &options = {},
                             const SourceMap *sources = nullptr);
// Adds the optional address table to a generated artifact set: <module>.address_table.json and
// <module>.address_table.csv list each initiator's routing windows (base, size, target, local
// base, access, exclusive support, default target), read from the map alone. The bundle lists both
// files with role address_table; every other artifact keeps its bytes. Fails without exactly one
// map and bundle, or when the set already has an address table.
Result<ArtifactSet> add_address_table(ArtifactSet artifacts);
} // namespace nocloom
