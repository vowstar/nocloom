import NetlistCheck.Verilog

/-!
# Reading the Verilog subset

A lexer and a recursive descent parser for exactly the syntax of
`Verilog.lean`. Anything else is an error: other directives, block comments,
attributes, escaped identifiers, keywords used as names, comment bytes outside
printable ASCII, non-decimal numbers, unsized numbers outside ranges, selects
and `STAGES`, operators outside the subset, and any other statement. Whitespace
between tokens is free here; `Check.lean` then requires that rendering the
result gives back the input byte for byte, so the parser cannot drop or invent
text unnoticed.

The parser is not proved correct. The byte round trip leaves only the
reading of each construct to trust, and that reading is the one-to-one
correspondence between the constructors of `Verilog.lean` and their text in
`render`.
-/

namespace NetlistCheck

inductive Tok where
  | ident (s : String)
  | kw (s : String)
  | num (n : Nat)
  | sized (w v : Nat)
  | sym (s : String)
  | directive (s : String)
  | comment (s : String)
  deriving DecidableEq, Repr, Inhabited

/-- The reserved words of IEEE 1364-2001 Annex B. None may name a net. -/
def keywords : List String :=
  ["always", "and", "assign", "automatic", "begin", "buf", "bufif0", "bufif1", "case", "casex",
   "casez", "cell", "cmos", "config", "deassign", "default", "defparam", "design", "disable",
   "edge", "else", "end", "endcase", "endconfig", "endfunction", "endgenerate", "endmodule",
   "endprimitive", "endspecify", "endtable", "endtask", "event", "for", "force", "forever",
   "fork", "function", "generate", "genvar", "highz0", "highz1", "if", "ifnone", "incdir",
   "include", "initial", "inout", "input", "instance", "integer", "join", "large", "liblist",
   "library", "localparam", "macromodule", "medium", "module", "nand", "negedge", "nmos", "nor",
   "noshowcancelled", "not", "notif0", "notif1", "or", "output", "parameter", "pmos", "posedge",
   "primitive", "pull0", "pull1", "pulldown", "pullup", "pulsestyle_onevent",
   "pulsestyle_ondetect", "rcmos", "real", "realtime", "reg", "release", "repeat", "rnmos",
   "rpmos", "rtran", "rtranif0", "rtranif1", "scalared", "showcancelled", "signed", "small",
   "specify", "specparam", "strong0", "strong1", "supply0", "supply1", "table", "task", "time",
   "tran", "tranif0", "tranif1", "tri", "tri0", "tri1", "triand", "trior", "trireg", "unsigned",
   "use", "vectored", "wait", "wand", "weak0", "weak1", "while", "wire", "wor", "xnor", "xor"]

def isIdentStart (c : UInt8) : Bool :=
  (c ≥ 'a'.toUInt8 && c ≤ 'z'.toUInt8) || (c ≥ 'A'.toUInt8 && c ≤ 'Z'.toUInt8) || c == '_'.toUInt8

def isDigit (c : UInt8) : Bool := c ≥ '0'.toUInt8 && c ≤ '9'.toUInt8

def isIdentChar (c : UInt8) : Bool := isIdentStart c || isDigit c

def isSpace (c : UInt8) : Bool :=
  c == ' '.toUInt8 || c == '\n'.toUInt8 || c == '\t'.toUInt8 || c == '\r'.toUInt8

def ascii (b : ByteArray) (start stop : Nat) : String :=
  String.mk ((List.range (stop - start)).map fun k => Char.ofNat (b.get! (start + k)).toNat)

def digits (b : ByteArray) (start stop : Nat) : Nat :=
  (List.range (stop - start)).foldl (fun n k => n * 10 + ((b.get! (start + k)).toNat - 48)) 0

/-- Single-character symbols; `==` and `<=` are read as one token. -/
def symbols : List Char := ['(', ')', '[', ']', '{', '}', ',', ';', ':', '?', '~', '&', '|', '-',
  '<', '=', '.', '#', '@', '!']

/-- The first index from `j` on whose byte fails `p`. -/
def scan (b : ByteArray) (p : UInt8 → Bool) (j : Nat) : Nat := Id.run do
  let mut k := j
  while k < b.size && p (b.get! k) do k := k + 1
  return k

def lex (text : String) : Except String (Array Tok) := do
  let b := text.toUTF8
  let mut i := 0
  let mut out : Array Tok := #[]
  while i < b.size do
    let c := b.get! i
    if isSpace c then
      i := i + 1
    else if c == '/'.toUInt8 && i + 1 < b.size && b.get! (i + 1) == '/'.toUInt8 then
      let stop := scan b (· != '\n'.toUInt8) (i + 2)
      if !(List.range (stop - (i + 2))).all (fun k =>
          let x := b.get! (i + 2 + k)
          x ≥ ' '.toUInt8 && x ≤ '~'.toUInt8) then
        throw s!"byte {i}: a comment holds a byte outside printable ASCII"
      out := out.push (.comment (ascii b (i + 2) stop))
      i := stop
    else if c == '`'.toUInt8 then
      let stop := scan b isIdentChar (i + 1)
      out := out.push (.directive (ascii b (i + 1) stop))
      i := stop
    else if isIdentStart c then
      let stop := scan b isIdentChar i
      let s := ascii b i stop
      out := out.push (if keywords.contains s then .kw s else .ident s)
      i := stop
    else if isDigit c then
      let stop := scan b isDigit i
      let w := digits b i stop
      if stop + 1 < b.size && b.get! stop == '\''.toUInt8 then
        if b.get! (stop + 1) != 'd'.toUInt8 then throw s!"byte {stop}: only 'd literals"
        let vstop := scan b isDigit (stop + 2)
        if vstop == stop + 2 then throw s!"byte {stop}: literal without digits"
        out := out.push (.sized w (digits b (stop + 2) vstop))
        i := vstop
      else
        out := out.push (.num w)
        i := stop
    else
      let ch := Char.ofNat c.toNat
      if (ch == '=' || ch == '<') && i + 1 < b.size && b.get! (i + 1) == '='.toUInt8 then
        out := out.push (.sym (String.mk [ch, '=']))
        i := i + 2
      else if ch == '!' && i + 1 < b.size && b.get! (i + 1) == '='.toUInt8 then
        throw s!"byte {i}: operator != is outside the subset"
      else if symbols.contains ch then
        out := out.push (.sym (String.singleton ch))
        i := i + 1
      else
        throw s!"byte {i}: character {c} is outside the subset"
  return out

/-! ## Parser -/

abbrev P := StateT Nat (ExceptT String (ReaderT (Array Tok) Id))

def peek : P (Option Tok) := do return (← read)[(← get)]?

def next : P Tok := do
  match ← peek with
  | some t => modify (· + 1); return t
  | none => throw "unexpected end of text"

def fail {α} (what : String) : P α := do
  throw s!"token {← get}: expected {what}, found {repr (← peek)}"

def expectSym (s : String) : P Unit := do
  if (← peek) == some (.sym s) then modify (· + 1) else fail s!"'{s}'"

def expectKw (s : String) : P Unit := do
  if (← peek) == some (.kw s) then modify (· + 1) else fail s!"'{s}'"

def isSym (s : String) : P Bool := do return (← peek) == some (.sym s)

def isKw (s : String) : P Bool := do return (← peek) == some (.kw s)

def ident : P Ident := do
  match ← peek with
  | some (.ident s) => modify (· + 1); return Ident.ofString s
  | _ => fail "an identifier"

def number : P Nat := do
  match ← peek with
  | some (.num v) => modify (· + 1); return v
  | _ => fail "a number"

/-- `[h:0]` after `wire` or `reg`, or nothing for one bit. -/
def width : P Nat := do
  if ← isSym "[" then
    modify (· + 1)
    let h ← number
    expectSym ":"
    let l ← number
    expectSym "]"
    if l != 0 || h == 0 then fail "a range [h:0] with h > 0"
    return h + 1
  return 1

def binOp : P BinOp := do
  match ← next with
  | .sym "<" => return .lt
  | .sym "==" => return .eq
  | .sym "&" => return .and
  | .sym "|" => return .or
  | .sym "-" => return .sub
  | _ => modify (· - 1); fail "an operator of the subset"

partial def expr : P Expr := do
  match ← next with
  | .ident s =>
    let x := Ident.ofString s
    if ← isSym "[" then
      modify (· + 1)
      let h ← number
      expectSym ":"
      let l ← number
      expectSym "]"
      return .part x h l
    return .id x
  | .sized w v => return .lit w v
  | .sym "{" =>
    let a ← expr
    expectSym ","
    let b ← expr
    expectSym "}"
    return .cat a b
  | .sym "(" =>
    if ← isSym "~" then
      modify (· + 1)
      let a ← expr
      expectSym ")"
      return .not a
    let a ← expr
    if ← isSym "?" then
      modify (· + 1)
      let x ← expr
      expectSym ":"
      let y ← expr
      expectSym ")"
      return .cond a x y
    let op ← binOp
    let b ← expr
    expectSym ")"
    return .bin op a b
  | _ => modify (· - 1); fail "an expression"

def conn : P Conn := do
  let x ← ident
  if ← isSym "[" then
    modify (· + 1)
    let b ← number
    expectSym "]"
    return .bit x b
  return .whole x

/-- `.pin(` value `)` -/
def pin {α} (name : String) (value : P α) : P α := do
  expectSym "."
  match ← next with
  | .ident s => if s != name then fail s!"pin {name}"
  | _ => fail s!"pin {name}"
  expectSym "("
  let v ← value
  expectSym ")"
  return v

/-- `q <= d;` with `d` a net, or with a sized literal when `literal`. -/
def nonblocking (literal : Bool) : P (Ident × Sum (Nat × Nat) Ident) := do
  let q ← ident
  expectSym "<="
  let v ← if literal then
      match ← next with
      | .sized w v => pure (.inl (w, v))
      | _ => modify (· - 1); fail "a sized literal"
    else .inr <$> ident
  expectSym ";"
  return (q, v)

def many {α} (stop : P Bool) (one : P α) (acc : Array α := #[]) : P (Array α) := do
  let mut acc := acc
  repeat
    if ← stop then break
    acc := acc.push (← one)
  return acc

def loads (stop : P Bool) : P (List (Ident × Ident)) := do
  let xs ← many stop do
    let (q, v) ← nonblocking false
    match v with
    | .inr d => pure (q, d)
    | .inl _ => fail "a net"
  return xs.toList

def always : P Item := do
  expectKw "always"
  expectSym "@"
  expectSym "("
  expectKw "posedge"
  let c ← ident
  let neg ← isKw "or"
  let r? ← if neg then
      modify (· + 1)
      expectKw "negedge"
      some <$> ident
    else pure none
  expectSym ")"
  expectKw "begin"
  if ← isKw "if" then
    modify (· + 1)
    expectSym "("
    expectSym "!"
    let r ← ident
    if r? != none && r? != some r then fail "the reset of the event list"
    expectSym ")"
    expectKw "begin"
    let clears ← many (isKw "end") do
      let (q, v) ← nonblocking true
      match v with
      | .inl (w, value) => pure (Clear.mk q w value)
      | .inr _ => fail "a sized literal"
    expectKw "end"
    expectKw "else"
    expectKw "begin"
    let ls ← loads (isKw "end")
    expectKw "end"
    expectKw "end"
    return .resetBlock c r neg clears.toList ls
  if neg then fail "a reset branch"
  let ls ← loads (isKw "end")
  expectKw "end"
  return .holdBlock c ls

def item : P Item := do
  match ← peek with
  | some (.kw "wire") | some (.kw "reg") =>
    let k := if (← next) == .kw "reg" then Kind.reg else .wire
    let w ← width
    let x ← ident
    expectSym ";"
    return .decl ⟨k, w, x⟩
  | some (.kw "assign") =>
    modify (· + 1)
    let x ← ident
    expectSym "="
    let e ← expr
    expectSym ";"
    return .assign x e
  | some (.kw "always") => always
  | some (.ident "nocloom_clock_gate") =>
    modify (· + 1)
    let inst ← ident
    expectSym "("
    let c ← pin "clock_in" ident
    expectSym ","
    let e ← pin "enable" ident
    expectSym ","
    let t ← pin "test" ident
    expectSym ","
    let o ← pin "clock_out" ident
    expectSym ")"
    expectSym ";"
    return .gate inst c e t o
  | some (.ident m) =>
    if m != "nocloom_synchronizer" && m != "nocloom_synchronizer_sync_reset" then
      fail "a statement of the subset"
    modify (· + 1)
    expectSym "#"
    expectSym "("
    let k ← pin "STAGES" number
    expectSym ")"
    let inst ← ident
    expectSym "("
    let c ← pin "clock" ident
    expectSym ","
    let r ← pin "reset_n" ident
    expectSym ","
    let d ← pin "d" conn
    expectSym ","
    let q ← pin "q" conn
    expectSym ")"
    expectSym ";"
    return .sync m k inst c r d q
  | _ => fail "a statement of the subset"

def portDecl : P PortDecl := do
  let input ← match ← next with
    | .kw "input" => pure true
    | .kw "output" => pure false
    | _ => modify (· - 1); fail "input or output"
  expectKw "wire"
  let w ← width
  let x ← ident
  return ⟨input, w, x⟩

def directive (value : String) : P Unit := do
  if (← next) != .directive "default_nettype" then fail "`default_nettype"
  match ← next with
  | .ident v => if v != value then fail s!"`default_nettype {value}"
  | .kw v => if v != value then fail s!"`default_nettype {value}"
  | _ => fail s!"`default_nettype {value}"

def module : P Module := do
  let comments ← many (do return !(match ← peek with | some (.comment _) => true | _ => false))
    (do match ← next with | .comment c => pure c | _ => fail "a comment")
  directive "none"
  expectKw "module"
  let name ← match ← next with
    | .ident s => pure s
    | _ => fail "a module name"
  expectSym "("
  let first ← portDecl
  let ports ← many (do return !(← isSym ",")) (do expectSym ","; portDecl) #[first]
  expectSym ")"
  expectSym ";"
  let body ← many (isKw "endmodule") item
  expectKw "endmodule"
  directive "wire"
  if (← peek).isSome then fail "the end of the text"
  return ⟨comments, name, ports, body⟩

def parseTokens (toks : Array Tok) : Except String Module :=
  match ((module.run 0).run).run toks with
  | .ok (m, _) => .ok m
  | .error e => .error e

def parse (text : String) : Except String Module := do
  parseTokens (← lex text)

end NetlistCheck
