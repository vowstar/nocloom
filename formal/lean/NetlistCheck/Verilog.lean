/-!
# The Verilog-2001 subset that NocLoom prints

The abstract syntax covers exactly what `src/backends/verilog/emitter.cpp`
prints for the top module: comment lines, `` `default_nettype ``, one module
with `wire` ports, `wire` and `reg` declarations, continuous assignments,
clock gate and synchronizer cell instances, and `always` blocks of two fixed
shapes. `render` gives the text the printer writes for a module, and
`Parse.lean` reads text back into this syntax.

## Expression meaning

`ev` follows IEEE 1364-2001 for the operators of the subset, on two-valued
unsigned bit vectors, with one restriction: it is defined only when each
context-determined operator (`~`, `&`, `|`, `-` and the two arms of `?:`) has
operands of equal width, both operands of `<` and `==` have equal width, the
condition of `?:` has width 1, and a part-select names a vector inside its
range. A continuous assignment holds only when its right side has the width
of its left side (`Holds`). Under these rules the context width of every
subexpression equals its self-determined width (IEEE 1364-2001 5.4.1 and
5.4.2), so no operand is extended or truncated, and `ev` is the standard's
value. Anything else is outside the subset and the check rejects it.

Values are naturals below `2 ^ width`: the meaning is two-valued. Every net
here has exactly one driver, so `z` does not arise. `x` is not modeled: the
theorems quantify over every 0 or 1 value of inputs and registers, which
covers a register without reset after its first load, but not the `x` it
holds in simulation before that.

## Registers and cells

`Check.lean` reads the other constructs as follows and compares them with the
netlist's states, clock gates and synchronizer stages.

| Construct | Reading |
| --- | --- |
| `resetBlock` with `negedge` | Each `q` loads its `d` at a rising clock edge and clears to its literal while the reset is low (IEEE 1364-2001 9.7, nonblocking assignment) |
| `resetBlock` without `negedge` | The same, except that `q` clears only at a rising clock edge while the reset is low |
| `holdBlock` | Each `q` loads its `d` at a rising clock edge and has no reset |
| `nocloom_clock_gate` | The clock gate cell: `clock_out` is `clock_in` gated by `enable` or `test` |
| `nocloom_synchronizer #(.STAGES(k))` | `k` registers in series from `d` to `q` on `clock`, cleared to 0 while `reset_n` is low, as the reset kind of the netlist says |

These readings are what the generated cell models implement. A bound library
cell meets them only when it matches its binding (docs/technology-cells.md).
The check does not read the cell files.
-/

namespace NetlistCheck

/-- `nl_internal_v<n>` names value `n`; any other identifier is `name`. -/
inductive Ident where
  | net (n : Nat)
  | name (s : String)
  deriving DecidableEq, Repr, Inhabited, Hashable

def netPrefix : String := "nl_internal_v"

def Ident.render : Ident → String
  | .net n => netPrefix ++ toString n
  | .name s => s

/-- Decimal digits without a leading zero, or `0`. -/
def canonicalDigits (s : String) : Bool :=
  !s.isEmpty && s.all Char.isDigit && (s == "0" || s.front != '0')

/-- The identifier of a name: the `net` form exactly when `render` would give
the same text back. -/
def Ident.ofString (s : String) : Ident :=
  if s.startsWith netPrefix && canonicalDigits (s.drop netPrefix.length) then
    .net (s.drop netPrefix.length).toNat!
  else .name s

inductive BinOp where
  | lt | eq | and | or | sub
  deriving DecidableEq, Repr, Inhabited

def BinOp.render : BinOp → String
  | .lt => "<" | .eq => "==" | .and => "&" | .or => "|" | .sub => "-"

/-- Every operator application is parenthesized as the printer writes it, so
the syntax needs no precedence. -/
inductive Expr where
  | id (x : Ident)
  | lit (width value : Nat)
  | not (a : Expr)
  | bin (op : BinOp) (a b : Expr)
  | cond (c a b : Expr)
  | cat (a b : Expr)
  | part (x : Ident) (hi lo : Nat)
  deriving DecidableEq, Repr, Inhabited

def Expr.render : Expr → String
  | .id x => x.render
  | .lit w v => toString w ++ "'d" ++ toString v
  | .not a => "(~" ++ a.render ++ ")"
  | .bin op a b => "(" ++ a.render ++ " " ++ op.render ++ " " ++ b.render ++ ")"
  | .cond c a b => "(" ++ c.render ++ " ? " ++ a.render ++ " : " ++ b.render ++ ")"
  | .cat a b => "{" ++ a.render ++ ", " ++ b.render ++ "}"
  | .part x h l => x.render ++ "[" ++ toString h ++ ":" ++ toString l ++ "]"

inductive Kind where
  | wire | reg
  deriving DecidableEq, Repr, Inhabited

structure Decl where
  kind : Kind
  width : Nat
  name : Ident
  deriving DecidableEq, Repr, Inhabited

structure PortDecl where
  input : Bool
  width : Nat
  name : Ident
  deriving DecidableEq, Repr, Inhabited

/-- A synchronizer pin connection: a whole net or one bit of it. -/
inductive Conn where
  | whole (x : Ident)
  | bit (x : Ident) (b : Nat)
  deriving DecidableEq, Repr, Inhabited

structure Clear where
  q : Ident
  width : Nat
  value : Nat
  deriving DecidableEq, Repr, Inhabited

inductive Item where
  | decl (d : Decl)
  | assign (lhs : Ident) (rhs : Expr)
  /-- `nocloom_clock_gate inst (.clock_in(), .enable(), .test(), .clock_out())` -/
  | gate (inst clockIn enable test clockOut : Ident)
  /-- `module #(.STAGES(stages)) inst (.clock(), .reset_n(), .d(), .q())` -/
  | sync (module : String) (stages : Nat) (inst clock reset : Ident) (d q : Conn)
  /-- `always @(posedge clock [or negedge reset]) begin if (!reset) begin
  clears end else begin loads end end`; `negedge` says whether the reset edge
  is in the event list. -/
  | resetBlock (clock reset : Ident) (negedge : Bool) (clears : List Clear)
      (loads : List (Ident × Ident))
  /-- `always @(posedge clock) begin loads end` -/
  | holdBlock (clock : Ident) (loads : List (Ident × Ident))
  deriving DecidableEq, Repr, Inhabited

structure Module where
  /-- Header comment lines, without the leading `//`. -/
  comments : Array String
  name : String
  ports : Array PortDecl
  body : Array Item
  deriving Repr, Inhabited

def range (w : Nat) : String := if w == 1 then "" else "[" ++ toString (w - 1) ++ ":0] "

def Conn.render : Conn → String
  | .whole x => x.render
  | .bit x b => x.render ++ "[" ++ toString b ++ "]"

def Item.render : Item → String
  | .decl d => (if d.kind == .reg then "reg " else "wire ") ++ range d.width ++ d.name.render ++ ";\n"
  | .assign x e => "assign " ++ x.render ++ " = " ++ e.render ++ ";\n"
  | .gate i c e t o =>
    "nocloom_clock_gate " ++ i.render ++ " (.clock_in(" ++ c.render ++ "), .enable(" ++
      e.render ++ "), .test(" ++ t.render ++ "), .clock_out(" ++ o.render ++ "));\n"
  | .sync m k i c r d q =>
    m ++ " #(.STAGES(" ++ toString k ++ ")) " ++ i.render ++ " (.clock(" ++ c.render ++
      "), .reset_n(" ++ r.render ++ "), .d(" ++ d.render ++ "), .q(" ++ q.render ++ "));\n"
  | .resetBlock c r neg clears loads =>
    "always @(posedge " ++ c.render ++ (if neg then " or negedge " ++ r.render else "") ++
      ") begin\n  if (!" ++ r.render ++ ") begin\n" ++
      String.join (clears.map fun x =>
        "    " ++ x.q.render ++ " <= " ++ toString x.width ++ "'d" ++ toString x.value ++ ";\n") ++
      "  end else begin\n" ++
      String.join (loads.map fun (q, d) => "    " ++ q.render ++ " <= " ++ d.render ++ ";\n") ++
      "  end\nend\n"
  | .holdBlock c loads =>
    "always @(posedge " ++ c.render ++ ") begin\n" ++
      String.join (loads.map fun (q, d) => "  " ++ q.render ++ " <= " ++ d.render ++ ";\n") ++
      "end\n"

def PortDecl.render (p : PortDecl) : String :=
  "  " ++ (if p.input then "input wire " else "output wire ") ++ range p.width ++ p.name.render

def Module.render (M : Module) : String := Id.run do
  let mut out := ""
  for c in M.comments do
    out := out ++ "//" ++ c ++ "\n"
  out := out ++ "`default_nettype none\nmodule " ++ M.name ++ " (\n"
  for h : k in [0:M.ports.size] do
    out := out ++ M.ports[k].render ++ (if k + 1 == M.ports.size then "\n);\n" else ",\n")
  for item in M.body do
    out := out ++ item.render
  return out ++ "endmodule\n`default_nettype wire\n"

/-! ## Meaning of expressions and assignments -/

/-- Result width and value of a binary operator on two operands of width `w`. -/
def BinOp.apply (w : Nat) : BinOp → Nat → Nat → Nat × Nat
  | .lt, x, y => (1, if x < y then 1 else 0)
  | .eq, x, y => (1, if x = y then 1 else 0)
  | .and, x, y => (w, x &&& y)
  | .or, x, y => (w, x ||| y)
  -- `x - y` modulo `2 ^ w` for `y < 2 ^ w`.
  | .sub, x, y => (w, (2 ^ w - y + x) % 2 ^ w)

/-- Width and value of an expression under declared widths `Γ` and net values
`σ`, or `none` outside the subset. -/
def ev (Γ : Ident → Option Nat) (σ : Ident → Nat) : Expr → Option (Nat × Nat)
  | .id x => (Γ x).map fun w => (w, σ x)
  | .lit w v => if 0 < w ∧ v < 2 ^ w then some (w, v) else none
  | .not a => (ev Γ σ a).map fun (w, x) => (w, 2 ^ w - 1 - x)
  | .bin op a b =>
    match ev Γ σ a, ev Γ σ b with
    | some (wa, x), some (wb, y) => if wa = wb then some (op.apply wa x y) else none
    | _, _ => none
  | .cond c a b =>
    match ev Γ σ c, ev Γ σ a, ev Γ σ b with
    | some (wc, z), some (wa, x), some (wb, y) =>
      if wc = 1 ∧ wa = wb then some (wa, if z ≠ 0 then x else y) else none
    | _, _, _ => none
  | .cat a b =>
    match ev Γ σ a, ev Γ σ b with
    | some (wa, x), some (wb, y) => some (wa + wb, x <<< wb ||| y)
    | _, _ => none
  | .part x h l =>
    match Γ x with
    | some w => if 1 < w ∧ l ≤ h ∧ h < w then some (h - l + 1, (σ x >>> l) % 2 ^ (h - l + 1)) else none
    | none => none

def Module.decls (M : Module) : List Decl :=
  M.body.toList.filterMap fun | .decl d => some d | _ => none

def Module.assigns (M : Module) : List (Ident × Expr) :=
  M.body.toList.filterMap fun | .assign x e => some (x, e) | _ => none

/-- `Γ` gives every declared port and net its declared width. -/
def Typed (M : Module) (Γ : Ident → Option Nat) : Prop :=
  (∀ p ∈ M.ports.toList, Γ p.name = some p.width) ∧ (∀ d ∈ M.decls, Γ d.name = some d.width)

/-- Each net holds a value of its width. -/
def InRange (Γ : Ident → Option Nat) (σ : Ident → Nat) : Prop :=
  ∀ x w, Γ x = some w → σ x < 2 ^ w

/-- Every continuous assignment holds, with equal widths on both sides. -/
def Holds (M : Module) (Γ : Ident → Option Nat) (σ : Ident → Nat) : Prop :=
  ∀ x e, (x, e) ∈ M.assigns → ∃ w, Γ x = some w ∧ ev Γ σ e = some (w, σ x)

end NetlistCheck
