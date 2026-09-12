#include "nocloom/config.hpp"
#include "nocloom/io.hpp"
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <set>

namespace nocloom::io {
namespace {
Diagnostic failure(std::string code, std::string message) {
  return {std::move(code), "", std::move(message), {}, {}};
}
} // namespace
Result<ParsedDefinition> load_definition(const std::filesystem::path &path) {
  if (path.extension() != ".yaml" && path.extension() != ".yml")
    return Result<ParsedDefinition>::failure(
        failure("NCL_INPUT_FORMAT", "Expected a .yaml or .yml file"));
  std::ifstream input(path, std::ios::binary);
  if (!input)
    return Result<ParsedDefinition>::failure(
        failure("NCL_IO_READ", "Cannot open input file: " + path.string()));
  std::string text(config::max_input_bytes + 1, '\0');
  input.read(text.data(), static_cast<std::streamsize>(text.size()));
  if (input.bad())
    return Result<ParsedDefinition>::failure(
        failure("NCL_IO_READ", "Cannot read input file: " + path.string()));
  text.resize(static_cast<std::size_t>(input.gcount()));
  return config::parse(text, path.string());
}

Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory) {
  if (artifacts.empty())
    return Result<WriteReport>::failure(failure("NCL_IO_PATH", "An artifact set cannot be empty"));
  std::set<std::string> paths;
  if (std::any_of(artifacts.begin(), artifacts.end(), [&](const auto &a) {
        return a.path.empty() || a.path == "." || a.path == ".." ||
               a.path.find_first_not_of(
                   "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") !=
                   std::string::npos ||
               !paths.insert(a.path).second;
      }))
    return Result<WriteReport>::failure(
        failure("NCL_IO_PATH", "Expected unique, portable file basenames"));
  std::error_code ec;
  auto directory = std::filesystem::absolute(output_directory, ec).lexically_normal();
  if (ec || output_directory.empty())
    return Result<WriteReport>::failure(failure("NCL_IO_PATH", "Invalid output directory"));
  std::filesystem::path current;
  for (const auto &component : directory) {
    current /= component;
    auto status = std::filesystem::symlink_status(current, ec);
    if (ec && ec != std::errc::no_such_file_or_directory)
      return Result<WriteReport>::failure(failure("NCL_IO_PATH", ec.message()));
    if (!ec && std::filesystem::is_symlink(status))
      return Result<WriteReport>::failure(
          failure("NCL_IO_PATH", "Symbolic links are not accepted in output paths"));
    ec.clear();
  }
  if (!std::filesystem::create_directory(directory, ec))
    return Result<WriteReport>::failure(
        failure(ec ? "NCL_IO_WRITE" : "NCL_IO_EXISTS",
                ec ? ec.message() : "Output directory already exists"));
  WriteReport report;
  for (const auto &a : artifacts) {
    const auto path = directory / a.path;
    auto *file = std::fopen(path.string().c_str(), "wbx");
    if (!file) {
      auto d =
          failure("NCL_IO_WRITE", "Cannot create output: " + a.path + ": " + std::strerror(errno));
      d.notes = report.written_paths;
      return Result<WriteReport>::failure(std::move(d));
    }
    report.written_paths.push_back(a.path);
    const auto count = std::fwrite(a.text.data(), 1, a.text.size(), file);
    const auto closed = std::fclose(file);
    if (count != a.text.size() || closed != 0) {
      auto d = failure("NCL_IO_WRITE", "Incomplete output: " + a.path);
      d.notes = report.written_paths;
      return Result<WriteReport>::failure(std::move(d));
    }
  }
  return Result<WriteReport>::success(std::move(report));
}
} // namespace nocloom::io
