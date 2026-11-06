# Technology cells

## Technology bindings

`cells.clock_gate` binds a positive-edge clock gate.

| Field | Meaning |
| --- | --- |
| `module` | Library module name |
| `clock` | Input clock pin |
| `enable` | Active-high enable pin |
| `test` | Optional test pin. Without it, enable receives enable OR test |
| `output` | Gated clock pin |
| `tie` | Map of extra input pins to Boolean constants |

`cells.synchronizer` binds a flip-flop or integrated synchronizer.

| Field | Meaning |
| --- | --- |
| `module` | Library module name |
| `stages` | 1 for a flip-flop to chain, or 2 to 4 for an integrated chain |
| `clock`, `data`, `reset`, `output` | Pin names |
| `reset_active` | Required reset polarity: `low` or `high` |
| `tie` | Map of extra input pins to Boolean constants |

Every chain, including two-stage reset release, must match the integrated cell
stage count (`NCL_CDC_STAGES_UNSUPPORTED`). The reset pin must clear the cell to
zero. With synchronous register reset, the bound cell must clear synchronously.

NocLoom does not inspect the technology library to check these properties.
Unbound wrappers contain behavioral models.

## Gate behavior

Gating changes no endpoint cycle. Holding test high gives the same port
behavior. The gate stays open through the second edge after its region becomes
idle without incoming work. Reset holds it open through the first edge after
release.

Synchronizers, reset-release state and exclusive replacement state use the
domain clock. A switch with no registers needs no gate.

Test and reset ports have no owning clock in the port map. Use
[map bindings](map-fields.md#clocks-cells-and-logs) for physical constraints,
placement and reliability checks.

## Wrapper interfaces

The gate wrapper is `nocloom_clock_gate`, with `clock_in`, `enable`, `test` and
`clock_out`. Its active-high enable and test are latched while the clock is low,
preserving whole high phases. The synchronizer wrapper takes `clock`, `reset_n`,
`d`, `q` and the stage-count parameter `STAGES`. Synchronous reset uses
`nocloom_synchronizer_sync_reset`.

A flip-flop binding is chained through `u_stage[k].u_cell`, with stage zero
nearest the input. An integrated binding uses `u_cell`. Instance paths come from
the map. The bundle marks unbound models as `cell_model`, bound wrappers as
`cell_wrapper`, and bound-cell models as `library_cell_model`.

Wrappers precede the top module. `library_cell_models` stays outside synthesis
and simulation source lists. For simulation, add either those models or your
library models. For synthesis, use the technology library.
