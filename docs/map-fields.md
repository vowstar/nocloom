# Map fields

See [definition](definition.md) for YAML fields and [outputs](outputs.md) for
formats. Profile-specific JSON records appear only for that profile.

## File and ports

| JSON path | Content |
| --- | --- |
| `format_version`, `generator`, `module` | Map format number, tool name and top module |
| `_license` | SPDX identifier, permission scope and the output and project license paths |
| `ports` | Port `name`, `direction`, `width`, `endpoint`, `signal` and owning `clock`. The clock is empty for ports with no owning clock |

## Configuration and resources

| JSON path | Content |
| --- | --- |
| `definition.name`, `definition.address_width`, `definition.initiators` | Definition name, global address width and initiator names |
| `definition.targets` | Target `name` and global bounds `begin` and `end` in bytes, excluding `end`. Optional `local`, `when` and `flow` give the local address, selection condition and target flow |
| `definition.transport` | `data_width`, `id_bits`, `len_bits`, `max_outstanding`, `transport_word_bytes` and optional `id_table_entries` |
| `definition.transport.packet_limits` | Per-pair packet size and boundary limits in bytes |
| `definition.transport.narrow_bursts` | Initiators with `narrow_bursts: merge_modifiable` |
| `definition.topology` | Request and response graphs with nodes, links, endpoint attachments, word widths and pipe settings. `generated` marks automatic graphs |
| `definition.connectivity` | Connected `source` and `target` pairs. `flow_pairs` lists admitted flow pairs when needed |
| `definition.topology.routes` | Explicit route entries with endpoint names, node names and any nonzero `source_path` and `target_path` |
| `network.request_routes`, `network.response_routes` | Route `entry`, `exit` and `hardware_nodes`, with `source_paths` and `target_path` when needed |
| `network.read_write`, `network.request_nodes`, `network.response_nodes` | Read and write route sharing and the generated node names by direction |
| `network.admission` | Per-source read and write route matches, with target and address plane signals when present |
| `network.resources` | Resource name, role and payload width, with capacity, domain and pipe settings where recorded |
| `network.storage` | Resource, payload and control signals, acquire and release signals, and clock where recorded |
| `network.adapters` | Width conversion stages with `name`, `segment`, `direction`, `kind`, `wide_bytes`, `narrow_bytes` and optional `clock_domain`. A `serializer` splits a word and a `deserializer` joins words |
| `network.formal` | Observation signal bindings for sources, targets and scheduling |
| `network.interleaved_targets` | Targets that can return interleaved read responses |
| `definition.endpoint_protocols` | Protocol names grouped by initiator and target. A common target protocol can appear as one name |
| `definition.endpoint_transactions` | Port ID and length widths, access modes and flow counts |
| `definition.endpoint_address_widths` | Address width by initiator, target and transport |
| `definition.endpoint_data_widths`, `definition.target_data_widths`, `definition.apb_target_data_widths` | Port and transport data widths, with separate records for changed target widths and APB target widths below 32 bits |
| `definition.address_inputs`, `definition.address_selectors`, `network.address_selectors` | Address selection ports, selector conditions and their signal bindings |
| `definition.default_targets` | Explicitly selected miss targets |
| `definition.address_selection_holes` | Coverage by initiator when the network has more than 16 selectors. `possible` is true if a selector assignment leaves an unmapped address or the search reaches its budget. `exact` is false when `possible` is based on the budget |
| `definition.outstanding` | Configured capacities by endpoint |
| `definition.outstanding.initiator_id_tables` | Ordering-key limits for multi-burst initiators where recorded |
| `definition.target_id_bits` | Effective target ID widths for multi-burst or target-monitor profiles |
| `definition.target_ids` | Target ID layouts in `ports`, compact flow indices, transaction `route_bits` and `route_fields`, and observation `record_fields`. See [target identifiers](transactions.md#target-identifiers) for when these fields appear |
| `definition.exclusive_targets` | Exclusive modes and owner flag order. Network monitors also list reservation counts and granule sizes in bytes |
| `definition.target_timeouts` | Target watchdog settings |
| `definition.ahb_bursts`, `definition.ahb_hresp_widths` | AHB burst shaping and response widths |
| `definition.ahb5_properties` | Protocol properties and endpoint options |
| `definition.apb_back_to_back_targets` | APB targets with back-to-back access enabled |
| `definition.fixed_bursts_single` | Dedicated targets serving FIXED beats individually |
| `definition.read_side_effects_targets` | Targets using exact packet shapes |
| `definition.target_attributes`, `definition.user_flags` | Target attribute bits and the endpoint mappings of user flags |
| `definition.response_user` | Data-user and response-user widths at the endpoints and in the network |

## Clocks, cells and logs

| JSON path | Content |
| --- | --- |
| `definition.reset_ports` | Reset port names in `name` |
| `definition.test_ports`, `definition.enable_ports`, `definition.ungate_ports` | Test, enable and ungate port names |
| `definition.domain`, `definition.clock_port`, `definition.reset_port` | Domain and its clock and reset ports in the single-domain record |
| `definition.clock_roots`, `definition.clock_domains` | Root and domain names, clocks and domain reset ports |
| `definition.test_port`, `definition.enable_port`, `definition.ungate_port`, `definition.gated`, `definition.reset_synchronizer` | Single-domain settings also used in domain entries when applicable |
| `definition.clock_domains[].clock_root`, `definition.clock_domains[].edge_parent`, `definition.clock_domains[].ratio` | Root clock, parent domain and clock rate ratio where configured |
| `definition.endpoint_clock_domains` | Clock-domain assignments by endpoint |
| `definition.clock_crossings` | Configured domain pairs and crossing options |
| `definition.clock_roots[].period_ps` | Known physical input period in ps |
| `definition.clock_domains[].period_ps` | Effective domain period in ps when known |
| `clock_period_sources` | Period source for each domain. `declared` gives an exact declaration. `promised` gives a minimum spacing declaration. `derived` gives a calculated value. `unknown` marks an unknown period. |
| `definition.register_reset` | Synchronous style when selected |
| `definition.synchronizer_stages` | Effective global crossing synchronizer setting |
| `definition.clock_gating` | `true` when automatic gating is enabled |
| `network.clock_gates` | Gate owner, clock domain, zone, register count and control nets |
| `network.cdc` | Object domains, cuts, registers, synchronizer stages and status of physical obligations. See [CDC timing constraints](timing-constraints.md) |
| `network.cdc.cuts[].unknown_period_domain` | Domain with an unknown period when exactly one crossing period is unknown and `depth_source` is `fallback` |
| `network.cdc.registers` | Register bindings or synchronizer instances and stage indices |
| `cells` | Technology bindings and actual instance paths |
| `definition.log_registers` | Bank names, local addresses (`base`) and `bank_bytes`. Banks record layout, log and logger indices, capabilities, and reachability when limited. Registers record names, offsets, lowest bits, widths, access, values or reset values, units and descriptions. The window also records error classes, access kinds and unmapped access behavior. With more than one bank, `banks` lists every bank |
| `definition.error_logs` | Register window, watched targets, alarm ports, stored fields, serial lengths, route values and audit flag names. The stored `class` field appears when `error_logging.capture_class` is true |

The map and bundle store whole periods as numbers when they fit in an unsigned
64-bit integer. Larger values use a decimal string. Fractional periods use
`whole`, `numerator` and `denominator` to represent `whole + numerator / denominator`
picoseconds. The same number and string formats apply to `whole`.

Crossing `depth_source` is `formula`, `fallback` or `explicit`. A synchronizer
entry identifies its instance and stage instead of a plain register name.
Clock-gate zone values are `protocol` and `transport`.

## Behavioral contracts

These names are relative to `definition.contract`.

| Field | Rules |
| --- | --- |
| `profile`, `routing`, `progress`, `capacity` | Exchange profile, route selection, progress and shared capacity |
| `connectivity`, `transport_address`, `address_transform`, `start_address` | Admission, restored addresses, target local address and burst start address |
| `burst_admission`, `attributes`, `omitted_attributes`, `target_attributes` | Burst limits and endpoint attribute handling |
| `unmapped`, `target_response`, `read_error`, `write_error`, `write_last_mismatch` | Error handling in the exchange |
| `write_data_unstrobed_bytes`, `user_flags`, `response_user` | Inactive byte values and user sidebands |
| `internal_failure_encoding` | Failure classes carried in responses inside the network. The `classified` format distinguishes `decode` and `target`. The `compact` format carries one failure class |
| `axi_initiator_failure_responses` | The response code sent at each AXI initiator port for each kind of failure. `decode_miss` covers address and connectivity misses, including unmapped parts of a decoded page |
| `endpoint_widths`, `endpoint_widths.narrow_bursts_default` | Conversion and narrow-burst handling |
| `packets`, `packets.narrow`, `packets.narrow_exclusive` | Packet limits and issue rules. Narrow packet rules include how target `exclusive` affects cuts |
| `ordering`, `target_id`, `target_outstanding` | Ordering, ID guarantees and target capacity |
| `lite_ingress.outstanding`, `lite_ingress.id_table` | Lite pipeline capacity and direction-key limits |
| `apb_ingress`, `ahb_ingress`, `axi3_ingress`, `axi3_egress` | Protocol adapter rules |
| `apb_targets`, `apb_target_words`, `apb_back_to_back` | APB target and word access behavior |
| `ahb_targets`, `ahb_targets.data_width`, `ahb_bursts`, `ahb5_properties`, `fixed_bursts`, `dedicated_dialects` | Dedicated target transformations and AHB5 sidebands |
| `target_timeout`, `exclusive`, `exclusive_access` | Failed target and monitor rules |
| `reset`, `crossing`, `physical_obligations` | Reset assertion, release, crossing coordination and timing obligations |
| `arbitration` | Effective policies, candidates, override origin, priority and waiting bounds |
| `arbitration.service`, `arbitration.qos` | Service conditions, initiator levels, mapped levels, shapers and schedulers |
| `arbitration.qos.pressure` | Scheduling pressure propagation |

Use `axi_initiator_failure_responses` for the code at an AXI port. Shared rules
can describe a decode failure as DECERR inside the network even when every
AXI port returns SLVERR. A log that records the class uses the internal encoding.

An output policy records `set_by` as `default` or `output`. Per-output wait
bounds require the consumer and endpoint duties in
[progress](timeout-and-progress.md).
