# Map fields

See [definition](definition.md) for YAML fields and [outputs](outputs.md) for
formats. Profile-specific JSON records appear only for that profile.

## Configuration and resources

| JSON path | Content |
| --- | --- |
| `definition.transport` | Resolved network widths, limits and sidebands |
| `definition.topology` | Configured graphs or the generated topology |
| `definition.connectivity` | Connected endpoint pairs. `flow_pairs` lists admitted flow pairs when needed |
| `definition.topology.routes` | Explicit entries with `source_path` and `target_path` |
| `network.request_routes`, `network.response_routes` | Actual node routes, with `source_paths` and `target_path` when needed |
| `network.admission` | Initiator read and write admission matches |
| `network.resources`, `network.storage` | Resource names, capacities and storage ownership |
| `definition.endpoint_protocols` | Protocol names by initiator and target |
| `definition.endpoint_transactions` | Port ID and length widths, access modes and flow counts |
| `definition.endpoint_data_widths`, `definition.target_data_widths` | Native widths and conversions, including changed target widths |
| `definition.default_targets` | Explicitly selected miss targets |
| `definition.outstanding` | Configured capacities by endpoint |
| `definition.outstanding.initiator_id_tables` | Ordering-key limits for multi-burst initiators where recorded |
| `definition.target_id_bits` | Effective target ID widths for multi-burst or target-monitor profiles |
| `definition.target_ids` | Compact flow indices, transaction `route_bits` and `route_fields`, observation `record_fields` |
| `definition.exclusive_targets` | Exclusive target configurations |
| `definition.target_timeouts` | Target watchdog settings |
| `definition.ahb_bursts`, `definition.ahb_hresp_widths` | AHB burst shaping and response widths |
| `definition.fixed_bursts_single` | Dedicated targets serving FIXED beats individually |
| `definition.read_side_effects_targets` | Targets using exact packet shapes |

## Clocks, cells and logs

| JSON path | Content |
| --- | --- |
| `definition.reset_ports` | Release mode and optional release domain |
| `definition.register_reset` | Synchronous style when selected |
| `definition.synchronizer_stages` | Effective global crossing synchronizer setting |
| `network.clock_gates` | Gate owner, protocol or transport zone, clock, register count and control nets |
| `network.cdc` | Crossings and integration obligations |
| `network.cdc.registers` | Register bindings or synchronizer instances and stage indices |
| `cells` | Technology bindings and actual instance paths |
| `definition.log_registers` | Register offsets, field positions, widths, access, values and descriptions |
| `definition.error_logs` | Log shapes, filters, serial lengths and route-field decoding |

Crossing depth origin is `formula`, `fallback` or `explicit`. A synchronizer
entry identifies its instance and stage instead of a plain register name.
Clock-gate zone values are `protocol` and `transport`.

## Behavioral contracts

These names are relative to `definition.contract`.

| Field | Rules |
| --- | --- |
| `connectivity`, `transport_address`, `write_data_unstrobed_bytes` | Admission, restored addresses and inactive byte values |
| `endpoint_widths`, `endpoint_widths.narrow_bursts_default` | Conversion and narrow-burst handling |
| `ordering`, `target_id`, `target_outstanding` | Ordering, ID guarantees and target capacity |
| `lite_ingress.outstanding`, `lite_ingress.id_table` | Lite pipeline capacity and direction-key limits |
| `apb_ingress`, `ahb_ingress`, `axi3_ingress`, `axi3_egress` | Protocol adapter rules |
| `ahb_targets.data_width`, `ahb_bursts`, `fixed_bursts` | Dedicated target transformations |
| `target_timeout`, `exclusive`, `exclusive_access` | Failed target and monitor rules |
| `reset` | Reset assertion, release and crossing coordination |
| `arbitration` | Effective policies, candidates, override origin, priority and waiting bounds |
| `arbitration.service`, `arbitration.qos` | Service conditions, initiator levels, mapped levels, shapers and schedulers |
| `arbitration.qos.pressure` | Scheduling pressure propagation |

An output policy records `set_by` as `default` or `output`. Per-output wait
bounds require the consumer and endpoint duties in
[progress](timeout-and-progress.md).
