#include "files.hpp"
#include "nocloom/config.hpp"
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <set>

namespace nocloom::io {
namespace {
Diagnostic failure(std::string code, std::string message) {
  return {std::move(code), "", std::move(message), {}, {}};
}
// The name with each ASCII letter in lower case, or with the case of each letter exchanged.
std::string recased(std::string name, bool exchange) {
  for (auto &c : name)
    if (c >= 'A' && c <= 'Z')
      c = static_cast<char>(c - 'A' + 'a');
    else if (exchange && c >= 'a' && c <= 'z')
      c = static_cast<char>(c - 'a' + 'A');
  return name;
}
// The pairs of artifact paths, in artifact order, that differ only in ASCII letter case.
std::vector<std::pair<std::string, std::string>> twin_pairs(const ArtifactSet &artifacts) {
  std::map<std::string, std::string> seen;
  std::vector<std::pair<std::string, std::string>> pairs;
  for (const auto &a : artifacts)
    if (const auto [at, added] = seen.try_emplace(recased(a.path, false), a.path);
        !added && at->second != a.path)
      pairs.emplace_back(at->second, a.path);
  return pairs;
}
// The pair that a message of NCL_IO_CASE names.
std::optional<std::pair<std::string, std::string>> twin_named(const ArtifactSet &artifacts,
                                                              const std::string &message) {
  const auto pairs = twin_pairs(artifacts);
  const auto named = std::ranges::find_if(pairs, [&](const auto &pair) {
    return message.starts_with("The output files " + pair.first + " and " + pair.second + " ");
  });
  if (named == pairs.end())
    return std::nullopt;
  return *named;
}
} // namespace

namespace detail {
std::optional<std::pair<std::string, std::string>> letter_case_twins(const ArtifactSet &artifacts) {
  auto pairs = twin_pairs(artifacts);
  if (pairs.empty())
    return std::nullopt;
  return pairs.front();
}

std::vector<Diagnostic> located(std::vector<Diagnostic> diagnostics, const ArtifactSet &artifacts,
                                const ParsedDefinition &source) {
  auto &d = diagnostics.front();
  const auto twins = twin_named(artifacts, d.message);
  if (!twins || d.code != "NCL_IO_CASE")
    return diagnostics;
  const auto names = [&](const auto &cell) {
    return cell && (twins->first == cell->module + ".v" || twins->second == cell->module + ".v");
  };
  const auto &cells = source.definition.cells;
  d.field = names(cells.synchronizer) ? "cells.synchronizer.module"
            : names(cells.clock_gate) ? "cells.clock_gate.module"
                                      : "name";
  // The nearest enclosing field that the source map knows.
  for (auto key = d.field; !d.location && !key.empty();) {
    if (const auto at = source.sources.find(key); at != source.sources.end())
      d.location = at->second;
    const auto end = key.find_last_of(".[");
    key = end == std::string::npos ? "" : key.substr(0, end);
  }
  return diagnostics;
}

// A directory that exists shows it by its own name: its parent finds it under the name with the
// case of each letter exchanged. Otherwise an entry of the parent with a letter shows it, unless
// the parent lists both names.
std::optional<bool> ignores_letter_case(const std::filesystem::path &directory) {
  namespace fs = std::filesystem;
  std::error_code ec;
  const auto parent = directory.parent_path();
  const auto found = [&](const std::string &name) {
    return fs::exists(fs::symlink_status(parent / recased(name, true), ec));
  };
  if (const auto name = directory.filename().string();
      recased(name, true) != name && fs::is_directory(fs::symlink_status(directory, ec)))
    return fs::is_directory(fs::symlink_status(parent / recased(name, true), ec)) &&
           fs::equivalent(directory, parent / recased(name, true), ec);
  std::set<std::string> names;
  for (fs::directory_iterator at(parent, ec), end; !ec && at != end; at.increment(ec))
    names.insert(at->path().filename().string());
  const auto letter = std::ranges::find_if(
      names, [](const std::string &name) { return recased(name, true) != name; });
  if (ec || letter == names.end())
    return std::nullopt;
  return !names.contains(recased(*letter, true)) && found(*letter);
}
} // namespace detail

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
  return detail::write_artifacts(artifacts, output_directory, detail::ignores_letter_case);
}

Result<WriteReport> write_artifacts(const ArtifactSet &artifacts,
                                    const std::filesystem::path &output_directory,
                                    const ParsedDefinition &source) {
  auto written = write_artifacts(artifacts, output_directory);
  if (written)
    return written;
  return Result<WriteReport>::failure(detail::located(written.diagnostics(), artifacts, source));
}

Result<WriteReport> detail::write_artifacts(const ArtifactSet &artifacts,
                                            const std::filesystem::path &output_directory,
                                            const LetterCaseProbe &probe) {
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
  // Letter case twins need a directory that keeps them apart. A new directory finds names as
  // its parent does, so the parent is tested before anything is created, and a parent without an
  // entry to test is tested by the name of the new directory. Where neither test can tell, the
  // files are written as before: each file is opened for exclusive creation, so no output ever
  // replaces another, and a twin that the directory finds under its sibling's name stops there.
  const auto twins = letter_case_twins(artifacts);
  const auto leaf = directory.has_filename() ? directory : directory.parent_path();
  const auto refused = [&](const std::pair<std::string, std::string> &pair, std::string tail) {
    return failure("NCL_IO_CASE", "The output files " + pair.first + " and " + pair.second +
                                      " differ only in letter case, and the output directory "
                                      "holds such names as one file" +
                                      std::move(tail));
  };
  // An output directory that is present keeps the diagnostic that it is present.
  const bool absent =
      std::filesystem::symlink_status(leaf, ec).type() == std::filesystem::file_type::not_found;
  auto ignored = twins && absent ? probe(leaf) : std::optional(false);
  if (ignored.value_or(false))
    return Result<WriteReport>::failure(refused(*twins, ""));
  if (!std::filesystem::create_directory(directory, ec))
    return Result<WriteReport>::failure(
        failure(ec ? "NCL_IO_WRITE" : "NCL_IO_EXISTS",
                ec ? ec.message() : "Output directory already exists"));
  if (!ignored && probe(leaf).value_or(false))
    return Result<WriteReport>::failure(refused(*twins, ". The new output directory is empty"));
  WriteReport report;
  std::map<std::string, std::string> written;
  for (const auto &a : artifacts) {
    const auto path = directory / a.path;
    auto *file = std::fopen(path.string().c_str(), "wbx");
    if (!file) {
      const auto error = errno;
      const auto sibling = written.find(recased(a.path, false));
      auto d =
          error == EEXIST && sibling != written.end()
              ? refused({sibling->second, a.path}, ". The files written before " + a.path + " stay")
              : failure("NCL_IO_WRITE",
                        "Cannot create output: " + a.path + ": " + std::strerror(error));
      d.notes = report.written_paths;
      return Result<WriteReport>::failure(std::move(d));
    }
    report.written_paths.push_back(a.path);
    written.emplace(recased(a.path, false), a.path);
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
