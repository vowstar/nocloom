import NetlistCheck.Check

/-!
`netlist_check DESIGN.v DESIGN.netlist.txt`: exit 0 when `check` holds for the
two files, 1 with the reason otherwise, 2 on a usage or read error.
-/

open NetlistCheck

def main (args : List String) : IO UInt32 := do
  match args with
  | [verilog, netlist] =>
    let v ← IO.FS.readFile verilog
    let n ← IO.FS.readFile netlist
    if check v n then
      IO.println s!"PASS {verilog}"
      return 0
    IO.println s!"FAIL {verilog}: {(explain v n).getD "check failed"}"
    return 1
  | _ =>
    IO.eprintln "usage: netlist_check DESIGN.v DESIGN.netlist.txt"
    return 2
