"""Bounded port and transport progress for the single-beat transaction fixture."""
import re

# The assertions at which each control fails. Silencing the transport response
# entry also breaks the context step, which can fail first.
CONTROLS = {
    "no_source": {"source_ar_sends", "source_aw_sends"},
    "no_issue": {"request_ar_issues", "request_aw_issues"},
    "no_answer": {"target_r_answers", "target_b_answers"},
    "no_forward": {"response_r_passes", "response_b_passes", "context_steps"},
    "no_return": {"response_r_returns", "response_b_returns"},
}


def progress(monitor):
    signal = monitor.sig
    reset = monitor.reset

    def property(name, kind, condition, schedule):
        monitor.property(name, kind, f"{reset} && ({condition}) |-> {schedule}")

    for side, channels in (("src", ("r", "b")), ("tgt", ("ar", "aw", "w"))):
        for ch in channels:
            property(f"ready_{side}_{ch}", "assume", signal(side, ch + "valid"),
                     f"##[0:2] {signal(side, ch + 'ready')}")
    property("write_data_offer", "assume", "src_aw && !seen_src_w", f"##[0:2] {signal('src', 'wvalid')}")

    monitor.lines.append("wire tgt_write_issued = (tgt_aw && (seen_tgt_w || tgt_w)) || (tgt_w && seen_tgt_aw);")
    cases = (("r", "ar", "tgt_ar", "!req_write", "!rsp_write"),
             ("b", "aw", "tgt_write_issued", "req_write", "rsp_write"))
    for return_ch, request_ch, issued, req_kind, rsp_kind in cases:
        req = f"req_take && {req_kind}"
        rsp = f"rsp_take && {rsp_kind}"
        property(f"offer_{return_ch}", "assume", issued,
                 f"##[1:3] {signal('tgt', return_ch + 'valid')}")
        property(f"source_{request_ch}_sends", "assert", f"src_{request_ch}", f"##[0:8] ({req})")
        property(f"request_{request_ch}_issues", "assert", req, f"##[0:6] {issued}")
        property(f"target_{return_ch}_answers", "assert", issued, f"##[1:6] tgt_{return_ch}")
        property(f"response_{return_ch}_passes", "assert", f"tgt_{return_ch}", f"##[0:6] ({rsp})")
        property(f"response_{return_ch}_returns", "assert", rsp, f"##[0:6] src_{return_ch}")
        monitor.property(f"late_offer_{return_ch}", "cover",
                         f"{reset} && {issued} ##1 !{signal('tgt', return_ch + 'valid')} "
                         f"##[1:2] tgt_{return_ch}")


def silence(design, name):
    pattern = re.compile(rf"(?m)^(assign {name} = )[^;]+;$")
    design, count = pattern.subn(r"\g<1>1'b0;", design)
    if count != 1:
        raise RuntimeError(f"Expected exactly one driver of {name}")
    return design


def designs(rtl, monitor, instrument):
    progress(monitor)
    assertions = "\n".join(monitor.lines)
    missing_answer = "\n".join(line for line in monitor.lines
                               if not line.startswith(("offer_r:", "offer_b:")))
    faces = monitor.mapping["boundaries"]
    req_valid = faces["req/entry"]["valid"]
    rsp_valid = faces["rsp/entry"]["valid"]
    no_issue = silence(silence(rtl, monitor.sig("tgt", "arvalid")), monitor.sig("tgt", "awvalid"))
    no_return = silence(silence(rtl, monitor.sig("src", "rvalid")), monitor.sig("src", "bvalid"))
    return {
        "completion": instrument(rtl, assertions),
        "no_source": instrument(silence(rtl, f"nl_internal_v{req_valid}"), assertions),
        "no_issue": instrument(no_issue, assertions),
        "no_answer": instrument(rtl, missing_answer),
        "no_forward": instrument(silence(rtl, f"nl_internal_v{rsp_valid}"), assertions),
        "no_return": instrument(no_return, assertions),
    }
