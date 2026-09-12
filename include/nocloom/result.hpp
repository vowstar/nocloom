#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace nocloom {

struct SourceLocation {
  std::string label;
  unsigned line = 0;
  unsigned column = 0;
};

struct Diagnostic {
  std::string code;
  std::string field;
  std::string message;
  std::optional<SourceLocation> location;
  std::vector<std::string> notes;
};

template <typename T> class Result {
public:
  static Result success(T value) { return Result(std::move(value)); }
  static Result failure(std::vector<Diagnostic> diagnostics) {
    if (diagnostics.empty())
      throw std::logic_error("A failed result requires a diagnostic");
    return Result(std::move(diagnostics));
  }
  static Result failure(Diagnostic diagnostic) {
    return failure(std::vector<Diagnostic>{std::move(diagnostic)});
  }
  explicit operator bool() const noexcept { return std::holds_alternative<T>(storage_); }
  T &value() & { return std::get<T>(storage_); }
  const T &value() const & { return std::get<T>(storage_); }
  T &&value() && { return std::get<T>(std::move(storage_)); }
  const std::vector<Diagnostic> &diagnostics() const {
    if (*this) {
      static const std::vector<Diagnostic> empty;
      return empty;
    }
    return std::get<std::vector<Diagnostic>>(storage_);
  }

private:
  explicit Result(T value) : storage_(std::move(value)) {}
  explicit Result(std::vector<Diagnostic> diagnostics) : storage_(std::move(diagnostics)) {}
  std::variant<T, std::vector<Diagnostic>> storage_;
};

using Status = Result<std::monostate>;
} // namespace nocloom
