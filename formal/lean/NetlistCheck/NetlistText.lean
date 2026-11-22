import NetlistCheck.Netlist

/-!
# Reading the netlist text

The format that `nocloom generate --netlist` writes (docs/netlist.md,
format): `#` comment lines, then one record per line. Values, ports and
states are numbered by their order. Any other line is an error.
-/

namespace NetlistCheck

def natOf (s : String) : Except String Nat :=
  if !s.isEmpty && s.all Char.isDigit && (s == "0" || s.front != '0') then .ok s.toNat!
  else .error s!"not a decimal number: {s}"

def opOf (name : String) (args : List Nat) : Except String Op :=
  match name, args with
  | "input", [p] => .ok (.input p)
  | "constant", [l] => .ok (.const l)
  | "zero_extend", [a] => .ok (.zext a)
  | "unsigned_less", [a, b] => .ok (.ult a b)
  | "bit_not", [a] => .ok (.not a)
  | "bit_and", [a, b] => .ok (.and a b)
  | "bit_or", [a, b] => .ok (.or a b)
  | "select", [c, a, b] => .ok (.select c a b)
  | "equal", [a, b] => .ok (.eq a b)
  | "state", [k] => .ok (.state k)
  | "subtract", [a, b] => .ok (.sub a b)
  | "slice", [a, o] => .ok (.slice a o)
  | "concat", [a, b] => .ok (.concat a b)
  | "clock_gate", [c, e, t] => .ok (.gate c e t)
  | _, _ => .error s!"unknown operation or operand count: {name}"

/-- The records must come in this order. -/
def section? : String → Option Nat
  | "port" => some 0 | "value" => some 1 | "state" => some 2 | "driver" => some 3
  | "synchronizer" => some 4 | _ => none

def parseNetlist (text : String) : Except String Netlist := do
  let lines := (text.splitOn "\n").filter fun l => !l.startsWith "#"
  match lines with
  | "netlist 1" :: module :: reset :: rest =>
    let name ← match module.splitOn " " with
      | ["module", n] => pure n
      | _ => throw "expected: module <name>"
    let syncReset ← match reset with
      | "reset asynchronous" => pure false
      | "reset synchronous" => pure true
      | _ => throw "expected: reset asynchronous|synchronous"
    let mut N : Netlist := ⟨name, syncReset, #[], #[], #[], #[], #[]⟩
    let mut last := 0
    let mut ended := false
    for line in rest do
      if ended then
        if line != "" then throw "text after end"
        continue
      if line == "end" then
        ended := true
        continue
      let words := line.splitOn " "
      let kind := words.headD ""
      let some order := section? kind | throw s!"unknown record: {line}"
      if order < last then throw s!"record out of order: {line}"
      last := order
      match kind, words.tail with
      | "port", [dir, w, n] =>
        let input ← match dir with
          | "input" => pure true
          | "output" => pure false
          | _ => throw s!"bad port direction: {line}"
        N := { N with ports := N.ports.push ⟨input, ← natOf w, n⟩ }
      | "value", w :: op :: args =>
        let args ← args.mapM natOf
        N := { N with values := N.values.push ⟨← natOf w, ← opOf op args⟩ }
      | "state", [o, n, c] =>
        N := { N with states := N.states.push ⟨← natOf o, ← natOf n, ← natOf c, none⟩ }
      | "state", [o, n, c, r, v] =>
        N := { N with states := N.states.push ⟨← natOf o, ← natOf n, ← natOf c,
          some (← natOf r, ← natOf v)⟩ }
      | "driver", [p, v] => N := { N with drivers := N.drivers.push (← natOf p, ← natOf v) }
      | "synchronizer", [v] => N := { N with syncStages := N.syncStages.push (← natOf v) }
      | _, _ => throw s!"malformed record: {line}"
    if !ended then throw "missing end"
    return N
  | _ => throw "expected the header: netlist 1, module, reset"

end NetlistCheck
