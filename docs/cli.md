# CLI

The CLI validates YAML definitions, generates files and compares definitions or
maps. Definition inputs use the `.yaml` or `.yml` extension.

## Validation and generation

After you build the CLI, run these commands from the repository root. The output
directory must be new, its parent must exist, and its path must contain no
symbolic link.

```sh
build/nocloom validate examples/tree_apb.yaml
build/nocloom generate examples/tree_apb.yaml --out-dir build/generated
```

`validate` reports diagnostics without generating files. `generate` validates
and generates in memory before writing artifacts. It defaults to the Verilog
target. Other `--target` values are rejected. `--ipxact`, `--address-table` and
`--cdc-constraints` add the files described in [output files](outputs.md).
`--c-header` adds [C headers](outputs.md#c-headers) for address windows and
error log register offsets.

`--version` reports the generator version.

| Exit code | Meaning |
| --- | --- |
| 0 | Success, or equal diff inputs |
| 1 | Invalid definition, unsupported generation or invalid comparison input |
| 2 | Command syntax error |
| 3 | An `NCL_IO_` code, for example a read or write failure, an unsafe output path or an output directory that is present |
| 4 | Diff found changes |

A file failure can leave partial output. Its diagnostic notes list files created
before failure. The CLI does not overwrite or roll back that directory.

## Semantic diff

```sh
build/nocloom diff examples/scale_axi_12.yaml examples/scale_axi_12_short.yaml
```

Each input can be YAML or a generated map. YAML is generated in memory without
output files. The default scope compares format version, module, definition,
ports and resource names. `--full` compares all map fields except generator.
Lists match by name, endpoint pair, link ends or attachment endpoint, as
appropriate. A map input larger than 32 MiB or nested deeper than 32 levels
produces `NCL_RESOURCE_LIMIT`.

Shorthand and its equivalent expanded definition compare equal.

Each changed field prints one line. Prefix `+` means only in the new input, and
`-` means only in the old input. A change prefix uses `~`, with old and new
values or an order change. Output follows map order and has no timestamps or
machine paths. Unrecognized input formats produce `NCL_INPUT_FORMAT` or
`NCL_DIFF_MAP`.
