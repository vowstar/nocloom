"""TL-UH target behind gated endpoints in another clock domain."""

from nlverif.config import ENDPOINT_GATE_SETTINGS

def configuration(capacity, maximum, mode, sources=1, width=32, cdc=True, timeout=0):
    domains = [dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")]
    cfg = dict(
        schema_version=10, name="uh_gating", address_width=32,
        clock_gating=mode != "off",
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=domains,
        initiators=[dict(name=f"s{k}", clock_domain="main", protocol="axi4", data_width=width,
                         id_width=4, len_width=8, max_outstanding=OUTSTANDING,
                         default_target="t") for k in range(sources)],
        targets=[dict(name="t", clock_domain="far" if cdc else "main", protocol="tl_uh",
                      data_width=32, max_transfer_bytes=maximum, max_outstanding=capacity,
                      size_width=4, read_side_effects=True,
                      mappings=[dict(base="0x0", size="0x10000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    if mode != "off":
        for endpoint in cfg["initiators"] + cfg["targets"]:
            endpoint["clock_gating"] = ENDPOINT_GATE_SETTINGS[mode]
    if timeout:
        cfg["targets"][0]["timeout"] = dict(cycles=timeout, completion=False)
    if cdc:
        domains.append(dict(name="far", clock_port="clk_far", reset_port="reset_n",
                            test_port="test"))
        names = [s["name"] for s in cfg["initiators"]]
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint=n, node="q") for n in names],
                         exits=[dict(endpoint="t", node="q")]),
            response=dict(nodes=[dict(name="r", clock_domain="far")], links=[],
                          entries=[dict(endpoint="t", node="r")],
                          exits=[dict(endpoint=n, node="r") for n in names]))
    return cfg


# Requests each initiator can hold, not less than the largest context pool.
OUTSTANDING = 16

# Initiator and target clock periods in picoseconds, with the ratio class.
PAIRS = [
    (2000, 16000, "fast_to_slow"), (4000, 12000, "fast_to_slow"),
    (4000, 10000, "fast_to_slow"), (6000, 9000, "fast_to_slow"),
    (16000, 2000, "slow_to_fast"), (12000, 4000, "slow_to_fast"),
    (10000, 4000, "slow_to_fast"), (9000, 6000, "slow_to_fast"),
    (10000, 10007, "near_equal"), (10000, 9991, "near_equal"),
    (5000, 5003, "near_equal"), (8001, 8000, "near_equal"),
    (10000, 14142, "non_harmonic"), (7919, 10007, "non_harmonic"),
    (12347, 5431, "non_harmonic"), (3331, 9973, "non_harmonic"),
]
CLASSES = ("fast_to_slow", "slow_to_fast", "near_equal", "non_harmonic")

# Networks of the sweep: contexts, message limit, initiators, AXI data width
# and watchdog cycles. Each one runs in every gating mode.
NETWORKS = dict(
    k1=dict(capacity=1, maximum=1024, sources=1, width=32, timeout=0),
    k3=dict(capacity=3, maximum=64, sources=2, width=32, timeout=256),
    k16=dict(capacity=16, maximum=4096, sources=2, width=128, timeout=0),
)
MODES = ("independent", "combined", "transport")

# Selector, name, contract and the checks that can report the fault.
FAULTS = {
    1: ("target_gate_ignores_wake", "AXI.CLOCK.GATE",
        ("GATE_CLOSED_WITH_WAKE", "TARGET_GATED_WITH_PENDING")),
    2: ("target_idle_stuck", "AXI.CLOCK.GATE",
        ("TARGET_GATED_WITH_PENDING", "TARGET_IDLE_WITH_PENDING")),
    3: ("initiator_gate_opens_late", "AXI.CLOCK.GATE",
        ("GATE_CLOSED_WITH_WAKE", "INITIATOR_GATED_WITH_BUS_WORK")),
    # The network misses a response beat that the slave has sent.
    4: ("response_pulse_lost", "TL.ACCESS",
        ("STUCK_TRANSACTION", "TIMEOUT_SPURIOUS", "READ_DATA", "TL.4.2.RESPONSE")),
    5: ("read_beat_corrupted", "TL.ACCESS", ("READ_DATA",)),
    # The network misses its own request handshake and sends the beat again.
    6: ("request_pulse_lost", "TL.ACCESS",
        ("SOURCE_REUSE", "TL.6.4.SOURCE", "PUT_INTERLEAVED", "WRITE_TWICE", "PUT_MASK",
         "PUT_DATA", "MESSAGE_RANGE")),
}

def fault_edits(cfg):
    """Each fault as a condition, an anchor kind, a named net and its new value."""
    gate = "gate/target/t/protocol"
    source = "gate/source/s0/protocol"
    lost_d = "nlv_fault_sel==4 & nlv_arm_d & nl_t_t_d_valid & nl_t_t_d_ready"
    lost_a = "nlv_fault_sel==6 & nlv_arm_a & nl_t_t_a_valid & nl_t_t_a_ready"
    return [
        # The gate opens only for its remaining-edge count.
        ("nlv_fault_sel==1", "wire", gate + ".enable",
         lambda v, net: f"{net(gate + '.remaining')}!=2'd0"),
        ("nlv_fault_sel==2", "wire", gate + ".idle", lambda v, net: "1'b1"),
        # A gate that was closed stays closed for one more edge.
        ("nlv_fault_sel==3", "wire", source + ".enable", lambda v, net: f"({v}) & nlv_was_open"),
        # The target protocol clock loses the pulse of one D beat or of one A beat.
        ("nlv_fault_sel!=0", "gate", gate + ".clock",
         lambda v, net: f"{v} & ~({lost_d}) & ~({lost_a})"),
        ("nlv_fault_sel==5", "output", "nl_i_s0_rdata", lambda v, net: f"({v}) ^ (nlv_r_count==40)"),
    ], f"""
integer nlv_d_count=0, nlv_a_count=0, nlv_r_count=0;
reg nlv_was_open=0, nlv_arm_d=0, nlv_arm_a=0;
always @(posedge clk) begin
  nlv_was_open <= nlv_kept2;
  if(nl_i_s0_rvalid && nl_i_s0_rready) nlv_r_count <= nlv_r_count+1;
end
always @(posedge clk_far) begin
  if({lost_d}) nlv_arm_d <= 0;
  if({lost_a}) nlv_arm_a <= 0;
  if(nl_t_t_d_valid && nl_t_t_d_ready) begin
    nlv_d_count <= nlv_d_count+1;
    if(nlv_d_count==60) nlv_arm_d <= 1;
  end
  if(nl_t_t_a_valid && nl_t_t_a_ready) begin
    nlv_a_count <= nlv_a_count+1;
    if(nlv_a_count==40) nlv_arm_a <= 1;
  end
end
"""


CASE = dict(
    contracts=["TL.ACCESS", "AXI.CLOCK.GATE"],
    dut=dict(source="cli", config=configuration(3, 64, "independent", 2, 32, True, 256)),
    faults=[dict(id=name, contract=contract) for name, contract, _ in FAULTS.values()],
)
