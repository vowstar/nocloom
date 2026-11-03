/-
  GenericNoC.Import: helpers for generated instance files.

  `tools/gen_instance.py` turns a dependency-graph artifact into a `.lean` file.
  Large tables are stored as chunks of text because a long list literal exhausts
  the elaborator.  Nothing here needs a proof: the parsed list simply is the
  table the certificate is checked against.
-/
import GenericNoC.WormholeFast
import GenericNoC.WormholeDone
import GenericNoC.KernelCert

namespace Wormhole.Import

/-- Parse `u v;u v;` text, appending to `acc`. -/
def scanPairs (acc : Array (Nat × Nat)) (s : String) : Array (Nat × Nat) :=
  let step (st : Array (Nat × Nat) × Nat × Nat) (ch : Char) :=
    let (a, u, cur) := st
    if ch == ' ' then (a, cur, 0)
    else if ch == ';' then (a.push (u, cur), 0, 0)
    else (a, u, cur * 10 + (ch.toNat - 48))
  (s.foldl step (acc, 0, 0)).1

/-- Parse `r;r;` text, appending to `acc`. -/
def scanNats (acc : Array Nat) (s : String) : Array Nat :=
  let step (st : Array Nat × Nat) (ch : Char) :=
    let (a, cur) := st
    if ch == ';' then (a.push cur, 0) else (a, cur * 10 + (ch.toNat - 48))
  (s.foldl step (acc, 0)).1

/-- Parse `a b c;a b c;` text, appending to `acc`. -/
def scanTriples (acc : Array (Nat × Nat × Nat)) (s : String) : Array (Nat × Nat × Nat) :=
  let step (st : Array (Nat × Nat × Nat) × Nat × Nat × Nat) (ch : Char) :=
    let (r, a, b, cur) := st
    if ch == ' ' then (r, b, cur, 0)
    else if ch == ';' then (r.push (a, b, cur), 0, 0, 0)
    else (r, a, b, cur * 10 + (ch.toNat - 48))
  (s.foldl step (acc, 0, 0, 0)).1

def triplesOf (groups : List (List String)) : Array (Nat × Nat × Nat) :=
  groups.foldl (fun a chunks => chunks.foldl scanTriples a) #[]

def pairsOf (groups : List (List String)) : Array (Nat × Nat) :=
  groups.foldl (fun a chunks => chunks.foldl scanPairs a) #[]

def natsOf (groups : List (List String)) : Array Nat :=
  groups.foldl (fun a chunks => chunks.foldl scanNats a) #[]

/-- Look up the node of a pair key in a table sorted by key.  A wrong or missing
    entry can only make coverage fail or connect the wrong nodes, which the check
    against the graph then rejects. -/
def keyNode (arr : Array (Nat × Nat × Nat)) (a b : Nat) : Nat :=
  let rec go : Nat → Nat → Nat → Nat
    | 0, _, _ => 0
    | fuel + 1, lo, hi =>
      if lo < hi then
        let mid := (lo + hi) / 2
        match arr[mid]? with
        | none => 0
        | some (x, y, n) =>
          if x == a && y == b then n
          else if x < a || (x == a && y < b) then go fuel (mid + 1) hi
          else go fuel lo mid
      else 0
  go 64 0 arr.size

/-- Node map of an instance. -/
def nodeMap (heads grants : Array (Nat × Nat × Nat)) : Hold → Nat
  | .head p d => keyNode heads p d
  | .grant o p => keyNode grants o p
  | _ => 0

/-- Pairs from text, as a list. -/
def pairListOf (groups : List (List String)) : List (Nat × Nat) := (pairsOf groups).toList

/-- Numbers from text, as a list. -/
def natListOf (groups : List (List String)) : List Nat := (natsOf groups).toList

/-- Parse `a b c d e;` text into rows by output. -/
def rowsOfText (groups : List (List String)) : Array (List Row) :=
  let step (st : Array (List Row) × List Nat × Nat) (ch : Char) :=
    let (a, fields, cur) := st
    if ch == ' ' then (a, cur :: fields, 0)
    else if ch == ';' then
      match cur :: fields with
      | [other, after, before, input, o] =>
        let grown := if o < a.size then a else
          a ++ Array.replicate (o + 1 - a.size) []
        (grown.modify o (fun l => l ++ [Row.mk input before after other]), [], 0)
      | _ => (a, [], 0)
    else (a, fields, cur * 10 + (ch.toNat - 48))
  (groups.foldl (fun st chunks => chunks.foldl (fun st s => s.foldl step st) st) (#[], [], 0)).1

/-- A set of pairs in a sorted array, as a function.  A false answer can only make
    an instance's claim narrower, and the instance's checks read the same function. -/
def sortedPairSet (arr : Array (Nat × Nat)) (a b : Nat) : Bool :=
  let e := (a, b)
  EdgeTable.searchIn arr e 64 0 arr.size

/-- Upstream lists from `(slot, upstream)` pairs: the empty list where none is given.
    A wrong list fails `wfCheck`. -/
def upsArray (pairs : Array (Nat × Nat)) : Array (List Nat) :=
  let top := pairs.foldl (fun m e => max m e.1) 0 + 1
  pairs.foldl (fun a e => a.modify e.1 (fun l => e.2 :: l)) (Array.replicate top [])

/-- The same for small tables, which the kernel reads. -/
def upsList (pairs : List (Nat × Nat)) (p : Nat) : List Nat :=
  (pairs.filter (fun e => e.1 == p)).map (fun e => e.2)

/-- A set of pairs as a function. -/
def pairSet (l : List (Nat × Nat)) (a b : Nat) : Bool := l.contains (a, b)

/-- A set of numbers as a function. -/
def natSet (l : List Nat) (a : Nat) : Bool := l.contains a

/-- The certificate gives the no-cycle statement directly. -/
theorem no_cycle {t : EdgeTable} (h : t.checkRank = true) (u : Nat) : ¬ t.Path u u :=
  fun p => Nat.lt_irrefl _ (EdgeTable.Path.rank_lt h p)

end Wormhole.Import
