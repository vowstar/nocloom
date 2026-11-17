"""TL-UH target under sustained contention and bounded backpressure."""
import runpy
from pathlib import Path

configuration = runpy.run_path(
    str(Path(__file__).with_name("tilelink_uh_target_gating.py")))["configuration"]

# Initiator and target clock periods in picoseconds, with the ratio class.
PAIRS = [
    (10000, 10007, "near_equal"), (10000, 14142, "non_harmonic"),
    (9000, 6000, "slow_to_fast"), (6000, 9000, "fast_to_slow"),
]

CLASSES = ("near_equal", "non_harmonic", "slow_to_fast", "fast_to_slow")

# Contexts, message limit, initiators, AXI data width and watchdog cycles.
NETWORKS = dict(
    k1=dict(capacity=1, maximum=1024, sources=2, width=32, timeout=0),
    k3=dict(capacity=3, maximum=64, sources=2, width=32, timeout=0),
    k16=dict(capacity=16, maximum=4096, sources=4, width=32, timeout=0),
)

# Selector, name, contract and the checks that can report the fault.
FAULTS = {
    1: ("context_waits_for_all_others", "TL.ACCESS", ("STARVATION",)),
    # The context keeps its packet and can answer it again.
    2: ("context_never_released", "TL.ACCESS",
        ("STARVATION", "LATENCY_BOUND", "STUCK_TRANSACTION", "POOL_NOT_RESTORED", "POOL_COUNT",
         "POOL_NOT_EMPTY", "WRITE_RESPONSE_ORDER", "B_WITHOUT_REQUEST", "R_WITHOUT_REQUEST")),
    3: ("responses_accepted_rarely", "TL.ACCESS", ("LATENCY_BOUND",)),
}


def fault_edits(cfg):
    """Each fault as a condition, an anchor kind, a named net and its new value."""
    pool = "burst_service/t/tl."
    last = cfg["targets"][0]["max_outstanding"] - 1

    def others(net):
        # Another context holds a packet that does not wait for any context.
        return " | ".join(
            f"({net(f'{pool}context{k}.tl.busy')} & {net(f'{pool}context{k}.dependencies')}==0"
            f" & {net(f'{pool}context{k}.response_dependencies')}==0)" for k in range(last))

    return [
        ("nlv_fault_sel==1", "wire", f"{pool}context{last}.tl.candidate",
         lambda v, net: f"({v}) & ~({others(net)})"),
        ("nlv_fault_sel==2", "wire", pool + "context1.tl.release",
         lambda v, net: f"({v}) & ~nlv_keep"),
        # The target sees and accepts a response beat in one cycle of 2048.
        ("nlv_fault_sel==3", "input", "nl_t_t_d_valid", lambda v, net: f"{v} & nlv_slot==0"),
        ("nlv_fault_sel==3", "output", "nl_t_t_d_ready", lambda v, net: f"({v}) & nlv_slot==0"),
    ], """
integer nlv_release_count=0;
reg nlv_keep=0;
reg [10:0] nlv_slot=0;
always @(posedge clk_far) begin
  nlv_slot <= nlv_slot+1;
  if(nlv_kept1) begin
    nlv_release_count <= nlv_release_count+1;
    if(nlv_release_count==40) nlv_keep <= 1;
  end
end
"""


CASE = dict(
    contracts=["TL.ACCESS"],
    dut=dict(source="cli", config=configuration(3, 64, "independent", 2, 32, True, 0)),
    faults=[dict(id=name, contract=contract) for name, contract, _ in FAULTS.values()],
)
