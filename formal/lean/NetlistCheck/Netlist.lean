/-!
# The generator's netlist

A deep embedding of the hardware that NocLoom builds before it prints Verilog
(`src/core/hardware.hpp`): ports, values in operand-before-user order, states,
output drivers and synchronizer stages. `--netlist` writes it as text
(`NetlistText.lean` reads it back).

Values are natural numbers below `2 ^ width`. Each operation is the `BitVec`
operation of its width, so `Netlist.eval` reads as the bit-vector meaning of
the netlist. States and clock gates are leaves: their values come from the
sequential behavior, which this file does not model.
-/

namespace NetlistCheck

/-- One value's operation. Operand fields are value indices; `input` names a
port index, `const` holds the literal and `state` the state index. -/
inductive Op where
  | input (port : Nat)
  | const (lit : Nat)
  | zext (a : Nat)
  | ult (a b : Nat)
  | not (a : Nat)
  | and (a b : Nat)
  | or (a b : Nat)
  | select (c a b : Nat)
  | eq (a b : Nat)
  | state (k : Nat)
  | sub (a b : Nat)
  | slice (a offset : Nat)
  | concat (a b : Nat)
  | gate (clock enable test : Nat)
  deriving DecidableEq, Repr, Inhabited

structure Value where
  width : Nat
  op : Op
  deriving DecidableEq, Repr, Inhabited

structure Port where
  input : Bool
  width : Nat
  name : String
  deriving DecidableEq, Repr, Inhabited

/-- A register: `output` is its state value, `reset` the low-active reset value
index and the value it clears to. -/
structure State where
  output : Nat
  next : Nat
  clock : Nat
  reset : Option (Nat × Nat)
  deriving DecidableEq, Repr, Inhabited

structure Netlist where
  name : String
  syncReset : Bool
  ports : Array Port
  values : Array Value
  states : Array State
  /-- (output port, value) -/
  drivers : Array (Nat × Nat)
  syncStages : Array Nat
  deriving Repr, Inhabited

/-- The value operands an operation reads. -/
def Op.operands : Op → List Nat
  | .zext a | .not a => [a]
  | .ult a b | .and a b | .or a b | .eq a b | .sub a b | .slice a b | .concat a b => [a, b]
  | .select c a b | .gate c a b => [c, a, b]
  | .input _ | .const _ | .state _ => []

/-- States and clock gates take their value from sequential behavior. -/
def Op.isLeaf : Op → Bool
  | .state _ | .gate .. => true
  | _ => false

namespace Netlist

def width (N : Netlist) (i : Nat) : Nat := (N.values[i]?.map (·.width)).getD 0

def op? (N : Netlist) (i : Nat) : Option Op := N.values[i]?.map (·.op)

/-- The literal of a constant value, 0 for anything else. -/
def lit (N : Netlist) (i : Nat) : Nat :=
  match N.op? i with
  | some (.const l) => l
  | _ => 0

def isConst (N : Netlist) (i : Nat) : Bool :=
  match N.op? i with
  | some (.const _) => true
  | _ => false

def isGate (N : Netlist) (i : Nat) : Bool :=
  match N.op? i with
  | some (.gate ..) => true
  | _ => false

/-- `w` bits of `x`. -/
abbrev bv (w x : Nat) : BitVec w := BitVec.ofNat w x

/-- The combinational meaning of value `i`, given the input port values `inp`
and the leaf values `leaf` (states and clock gate outputs). -/
def eval (N : Netlist) (inp : Nat → Nat) (leaf : Nat → Nat) (i : Nat) : Nat :=
  match N.values[i]? with
  | none => 0
  | some v =>
    let w := v.width
    let arg := fun a => if _h : a < i then N.eval inp leaf a else 0
    match v.op with
    | .input p => (bv w (inp p)).toNat
    | .const l => (bv w l).toNat
    | .state _ | .gate .. => (bv w (leaf i)).toNat
    | .zext a => (BitVec.setWidth w (bv (N.width a) (arg a))).toNat
    | .ult a b => if (bv (N.width a) (arg a)).ult (bv (N.width a) (arg b)) then 1 else 0
    | .eq a b => if bv (N.width a) (arg a) = bv (N.width a) (arg b) then 1 else 0
    | .not a => (~~~bv w (arg a)).toNat
    | .and a b => (bv w (arg a) &&& bv w (arg b)).toNat
    | .or a b => (bv w (arg a) ||| bv w (arg b)).toNat
    | .sub a b => (bv w (arg a) - bv w (arg b)).toNat
    | .select c a b =>
      if bv 1 (arg c) = 1#1 then (bv w (arg a)).toNat else (bv w (arg b)).toNat
    | .slice a o => (BitVec.extractLsb' (N.lit o) w (bv (N.width a) (arg a))).toNat
    | .concat a b => (bv (N.width a) (arg a) ++ bv (N.width b) (arg b)).toNat
termination_by i

/-- The widest signal the generator builds. -/
def maxWidth : Nat := 1024

/-- A clock: an input, or a clock gate of a clock. -/
def isClock (N : Netlist) (i : Nat) : Bool :=
  match N.values[i]? with
  | some ⟨1, .input _⟩ => true
  | some ⟨1, .gate c _ _⟩ => if _h : c < i then N.isClock c else false
  | _ => false
termination_by i

/-- The structural rules of one value (`check_value` in the generator). -/
def valueOk (N : Netlist) (i : Nat) (v : Value) : Bool :=
  0 < v.width && v.width ≤ maxWidth &&
  v.op.operands.all (· < i) &&
  (match v.op with
    | .gate _ e t => !N.isGate e && !N.isGate t
    | op => op.operands.all fun a => !N.isGate a) &&
  match v.op with
  | .input p =>
    match N.ports[p]? with
    | some port => port.input && port.width == v.width
    | none => false
  | .const l => l < 2 ^ v.width
  | .zext a => N.width a < v.width
  | .ult a b | .eq a b => N.width a == N.width b && v.width == 1
  | .not a => N.width a == v.width
  | .and a b | .or a b | .sub a b => N.width a == v.width && N.width b == v.width
  | .select c a b => N.width c == 1 && N.width a == v.width && N.width b == v.width
  | .state k =>
    match N.states[k]? with
    | some s => s.output == i
    | none => false
  | .slice a o => N.isConst o && N.lit o + v.width ≤ N.width a
  | .concat a b => v.width == N.width a + N.width b
  | .gate c e t => v.width == 1 && N.isClock c && N.width e == 1 && N.width t == 1

def stateOk (N : Netlist) (k : Nat) (s : State) : Bool :=
  s.output < N.values.size && N.op? s.output == some (.state k) &&
  s.next < N.values.size && N.width s.next == N.width s.output &&
  s.clock < N.values.size && N.isClock s.clock &&
  match s.reset with
  | some (r, value) => r < N.values.size && N.width r == 1 && value < 2 ^ N.width s.output
  | none => true

def driverOk (N : Netlist) (d : Nat × Nat) : Bool :=
  d.2 < N.values.size &&
  match N.ports[d.1]? with
  | some port => !port.input && port.width == N.width d.2
  | none => false

/-- Every output port has exactly one driver. -/
def driversComplete (N : Netlist) : Bool :=
  let count := N.drivers.foldl (fun acc d => acc.modify d.1 (· + 1)) (Array.replicate N.ports.size 0)
  (List.range N.ports.size).all fun p =>
    match N.ports[p]? with
    | some port => if port.input then count[p]! == 0 else count[p]! == 1
    | none => false

def portsOk (N : Netlist) : Bool :=
  !N.ports.isEmpty && N.ports.all fun p => 0 < p.width && p.width ≤ maxWidth

/-- The structural rules the generator's `verify_structure` enforces and the
soundness proof relies on. -/
def wf (N : Netlist) : Bool :=
  N.portsOk &&
  (List.range N.values.size).all (fun i =>
    match N.values[i]? with
    | some v => N.valueOk i v
    | none => false) &&
  (List.range N.states.size).all (fun k =>
    match N.states[k]? with
    | some s => N.stateOk k s
    | none => false) &&
  N.drivers.all N.driverOk && N.driversComplete

end Netlist
end NetlistCheck
