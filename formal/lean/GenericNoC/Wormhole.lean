/-
  GenericNoC.Wormhole: a concrete wormhole transport as a transition system.

  This is the part of the architecture that decides hold-and-wait:

  * Each slot presents one head word.  Words behind the head wait, whatever
    their destination.  A target's read data and write responses share one
    slot, so they share one head.
  * An output is granted to one input slot at a time.  The grant is taken by a
    head word and stays until a word with `tail` set passes the output.
  * A word passes when its input holds the grant and the output's downstream
    has room.  Downstream is a slot of bounded capacity, a bounded pool of
    target contexts, or a free sink that only the environment can stall.
  * The environment adds words to endpoint slots.  It may interleave words for
    different destinations unless the slot is declared contiguous.

  `tail` is the release decision.  A slot may set `tail` to false only where
  `mayHold` allows it.  The safe policy is one of these two: every word ends
  its segment, or a word that does not end its segment is always followed by a
  word for the same destination.  A slot that breaks both is declared `cross`,
  and then the graph must carry the edge from its head to its held grant.
-/
import GenericNoC.Witness
import GenericNoC.EdgeTable

namespace Wormhole

/-- A word on the transport. `last` ends a transaction and frees a context. -/
structure Word where
  dest : Nat
  tail : Bool
  last : Bool
  tok : Nat
  deriving DecidableEq, Repr

/-- An instance.  Slots, outputs and destinations are plain numbers. -/
structure Config where
  /-- `(slot, destination, output)`: the output a head word takes. -/
  routes : List (Nat × Nat × Nat)
  /-- `(output, slot)`: the slot an output feeds.  No entry means a sink. -/
  nexts : List (Nat × Nat)
  /-- `(output, destination, slot)`: a word for this destination takes, at this sink,
      a context of that response slot.  A target of several flows has one row per
      flow, each with its own destination. -/
  pools : List (Nat × Nat × Nat)
  cap : Nat → Nat
  poolCap : Nat → Nat
  /-- Slots the environment adds words to. -/
  endpoints : List Nat
  /-- Whether a word for this destination may leave `tail` clear. -/
  mayHold : Nat → Nat → Bool
  /-- A clear `tail` is followed by a word for the same destination. -/
  contig : Nat → Bool
  /-- The instance does not claim that the slot's stream is coherent. -/
  cross : Nat → Bool
  /-- A destination answered by one slot at a time: a source whose single ID and
      single direction bind it to one target until its answers have arrived. -/
  bound : Nat → Bool
  /-- Every slot, strictly increasing. -/
  slots : List Nat
  /-- Steps a word for a destination still has to take from a slot. -/
  hops : Nat → Nat → Nat
  /-- Above every entry of `hops`. -/
  hopMax : Nat
  /-- An endpoint decides the end of a segment as a word leaves: it may stay open
      only if the next word is already behind it, with the same destination. -/
  ahead : Nat → Nat → Bool
  /-- Which destinations answer a request from this slot. -/
  answers : Nat → Nat → Bool
  /-- The slots upstream of a slot, itself included.  Checked by `wfCheck`. -/
  ups : Nat → List Nat

namespace Config

variable (c : Config)

def route (p d : Nat) : Option Nat :=
  (c.routes.find? (fun r => r.1 == p && r.2.1 == d)).map (fun r => r.2.2)

def next (o : Nat) : Option Nat := c.nexts.lookup o

def pool (o d : Nat) : Option Nat :=
  (c.pools.find? (fun e => e.1 == o && e.2.1 == d)).map (fun e => e.2.2)

theorem route_mem {c : Config} {p d o : Nat} (h : c.route p d = some o) : (p, d, o) ∈ c.routes := by
  simp only [route, Option.map_eq_some_iff] at h
  obtain ⟨r, hr, rfl⟩ := h
  have hm := List.mem_of_find?_eq_some hr
  have hp := List.find?_some hr
  simp only [Bool.and_eq_true, beq_iff_eq] at hp
  obtain ⟨r1, r2, r3⟩ := r
  simp only at hp
  rw [← hp.1, ← hp.2]
  exact hm

theorem lookup_mem {l : List (Nat × Nat)} {a b : Nat} (h : l.lookup a = some b) : (a, b) ∈ l := by
  induction l with
  | nil => simp at h
  | cons x xs ih =>
    obtain ⟨k, v⟩ := x
    simp only [List.lookup_cons] at h
    split at h
    · rename_i he
      have : a = k := by simpa using he
      subst this
      cases h
      simp
    · exact List.mem_cons_of_mem _ (ih h)

end Config

/-- Function update on numbers. -/
def upd {β : Type} (f : Nat → β) (a : Nat) (v : β) : Nat → β :=
  fun x => if x = a then v else f x

@[simp] theorem upd_same {β : Type} (f : Nat → β) (a : Nat) (v : β) : upd f a v a = v := by
  simp [upd]

theorem upd_ne {β : Type} (f : Nat → β) {a x : Nat} (v : β) (h : x ≠ a) : upd f a v x = f x := by
  simp [upd, h]

/-- A word at `r` can reach `p` along the routes. -/
inductive Upstream (c : Config) : Nat → Nat → Prop where
  | refl (p : Nat) : Upstream c p p
  | step {r d o x p} : c.route r d = some o → c.next o = some x → Upstream c x p → Upstream c r p

/-- Machine state. -/
structure St where
  q : Nat → List Word
  grant : Nat → Option Nat
  /-- The destination the stream is open toward after the last word entered. -/
  openIn : Nat → Option Nat
  /-- The destination the stream is open toward before the head word. -/
  openOut : Nat → Option Nat
  /-- Transactions that hold a context. -/
  holders : List Nat
  /-- The slot that accepted a transaction. -/
  src : Nat → Option Nat
  /-- Accepted transactions, by token. -/
  live : Nat → Bool
  /-- This token was accepted once.  A token is never accepted twice. -/
  used : Nat → Bool
  /-- The last request word was added: no more request words. -/
  closed : Nat → Bool
  /-- The last request word arrived: the target may answer. -/
  reqDone : Nat → Bool
  /-- Words this transaction may still add, fixed when it is accepted. -/
  budget : Nat → Nat
  /-- The response slot that owes this transaction its next words. -/
  home : Nat → Option Nat
  /-- The `(slot, output)` that fired in the step that led here. -/
  ev : Option (Nat × Nat)

/-- The empty network. -/
def St.empty : St where
  q := fun _ => []
  grant := fun _ => none
  openIn := fun _ => none
  openOut := fun _ => none
  holders := []
  src := fun _ => none
  live := fun _ => false
  used := fun _ => false
  closed := fun _ => false
  reqDone := fun _ => false
  budget := fun _ => 0
  home := fun _ => none
  ev := none

/-- The stream state after a word. -/
def nextOpen (w : Word) : Option Nat := if w.tail then none else some w.dest

/-- Contexts in use at a response slot: one per live transaction, however many words
    its request has.  A transaction that has ended uses none, by this definition. -/
def poolUse (σ : St) (r : Nat) : Nat :=
  (σ.holders.filter (fun k => σ.live k && σ.home k == some r)).length

/-- Room behind an output for a new transaction toward a destination. -/
def room (c : Config) (σ : St) (o d : Nat) : Prop :=
  match c.next o with
  | some r => (σ.q r).length < c.cap r
  | none =>
    match c.pool o d with
    | some r => poolUse σ r < c.poolCap r
    | none => True

/-- Room for this word: a later word of a request that holds a context needs no
    new one. -/
def fits (c : Config) (σ : St) (o : Nat) (w : Word) : Prop :=
  room c σ o w.dest ∨ (c.next o = none ∧ w.tok ∈ σ.holders)

/-- Room for the head word of a slot. -/
def fitsHead (c : Config) (σ : St) (p o : Nat) : Prop :=
  ∀ w, (σ.q p).head? = some w → fits c σ o w

theorem fitsHead_of_room {c : Config} {σ : St} {p o : Nat}
    (h : ∀ d, room c σ o d) : fitsHead c σ p o :=
  fun w _ => Or.inl (h w.dest)

/-- The slot whose head can hold up an output: downstream or context owner. -/
def blockerOf (c : Config) (o d : Nat) : Option Nat :=
  match c.next o with
  | some r => some r
  | none => c.pool o d

/-- The word leaves its slot; the grant ends if the word has `tail` set. -/
def pop (σ : St) (p o : Nat) (w : Word) : St :=
  { σ with
    q := upd σ.q p (σ.q p).tail
    openOut := upd σ.openOut p (nextOpen w)
    grant := if w.tail then upd σ.grant o none else σ.grant
    holders := if w.last = true ∧ σ.home w.tok = some p then σ.holders.erase w.tok else σ.holders
    ev := some (p, o) }

/-- The word arrives behind the output. -/
def place (c : Config) (σ : St) (o : Nat) (w : Word) : St :=
  match c.next o with
  | some r =>
    { σ with
      q := upd σ.q r (σ.q r ++ [w])
      openIn := upd σ.openIn r (nextOpen w) }
  | none =>
    match c.pool o w.dest with
    | some r =>
      { σ with
        holders := if σ.reqDone w.tok = true ∨ w.tok ∈ σ.holders then σ.holders
          else w.tok :: σ.holders
        home := if σ.reqDone w.tok = true then σ.home else upd σ.home w.tok (some r)
        reqDone := if w.tail then upd σ.reqDone w.tok true else σ.reqDone }
    | none =>
      { σ with
        live := if w.last then upd σ.live w.tok false else σ.live
        home := if w.last then upd σ.home w.tok none else σ.home }

/-- A word for this destination, from this slot, ends at a free sink, which is
    where a transaction ends. -/
inductive EndsFree (c : Config) : Nat → Nat → Prop where
  | sink {p d o} : c.route p d = some o → c.next o = none → c.pool o d = none → EndsFree c p d
  | step {p d o r} : c.route p d = some o → c.next o = some r → EndsFree c r d → EndsFree c p d

/-- The words a transaction may still add after this word.  `b` is what a new
    transaction declares when it is accepted. -/
def newBudget (σ : St) (w : Word) (b : Nat) : Nat :=
  if σ.live w.tok = true then σ.budget w.tok - 1 else b

/-- A slot that answers requests. -/
def isResponse (c : Config) (p : Nat) : Bool := c.pools.any (fun e => e.2.2 == p)

/-- The destination answers the slot that accepted the transaction. -/
def answersSrc (c : Config) (src : Option Nat) (d : Nat) : Bool :=
  match src with
  | some s => c.answers s d
  | none => false

/-- The state after the environment adds a word. -/
def injected (σ : St) (p : Nat) (w : Word) (b : Nat) : St :=
  { σ with
    q := upd σ.q p (σ.q p ++ [w])
    openIn := upd σ.openIn p (nextOpen w)
    live := upd σ.live w.tok true
    budget := upd σ.budget w.tok (newBudget σ w b)
    src := if σ.live w.tok = true then σ.src else upd σ.src w.tok (some p)
    used := upd σ.used w.tok true
    closed := if w.tail then upd σ.closed w.tok true else σ.closed
    ev := none }

/-- A slot has a word for this destination, or holds an output toward it. -/
def touches (c : Config) (σ : St) (p d : Nat) : Prop :=
  (∃ a, a ∈ σ.q p ∧ a.dest = d) ∨ (∃ o, c.route p d = some o ∧ σ.grant o = some p)

/-- One transition. -/
inductive Step (c : Config) : St → St → Prop where
  | stutter (σ : St) : Step c σ { σ with ev := none }
  /-- A head word takes a free output. -/
  | arb (σ : St) (p o : Nat) (w : Word)
      (hh : (σ.q p).head? = some w) (hr : c.route p w.dest = some o)
      (hf : σ.grant o = none) :
      Step c σ { σ with grant := upd σ.grant o (some p), ev := none }
  /-- A head word passes the output its slot holds. -/
  | fire (σ : St) (p o : Nat) (w : Word)
      (hh : (σ.q p).head? = some w) (hr : c.route p w.dest = some o)
      (hg : σ.grant o = some p) (hroom : fits c σ o w) (hloop : c.next o ≠ some p)
      (hcut : c.ahead p w.dest = true → w.tail = false →
        ∃ a, (σ.q p).tail.head? = some a ∧ a.dest = w.dest) :
      Step c σ (place c (pop σ p o w) o w)
  /-- An endpoint ends the segment at its head word: it may do this at any time, and
      must before the word can leave without the next word behind it. -/
  | cut (σ : St) (p : Nat) (w : Word)
      (he : p ∈ c.endpoints) (hh : (σ.q p).head? = some w) (hahead : c.ahead p w.dest = true)
      (ht : w.tail = false) :
      Step c σ { σ with
        q := upd σ.q p ({ w with tail := true } :: (σ.q p).tail)
        ev := none }
  /-- The environment adds a word to an endpoint. -/
  | inject (σ : St) (p : Nat) (w : Word) (b : Nat)
      (he : p ∈ c.endpoints) (hcap : (σ.q p).length < c.cap p)
      (hhold : w.tail = true ∨ c.mayHold p w.dest = true ∨ c.ahead p w.dest = true)
      (hcontig : c.contig p = true → ∀ d, σ.openIn p = some d → w.dest = d)
      (hrt : (c.route p w.dest).isSome = true)
      (hfresh : σ.live w.tok = false → σ.used w.tok = false)
      (hwhere : σ.live w.tok = true →
        (σ.src w.tok = some p ∧ (σ.closed w.tok = false ∨ c.pools = [])) ∨ σ.home w.tok = some p)
      (hresp : isResponse c p = true →
        σ.live w.tok = true ∧ σ.home w.tok = some p ∧ σ.reqDone w.tok = true ∧
          answersSrc c (σ.src w.tok) w.dest = true)
      (hbudget : σ.live w.tok = true → 0 < σ.budget w.tok)
      (hlast : w.last = true ↔ newBudget σ w b = 0)
      (hfinal : w.last = true → w.tail = true ∧ EndsFree c p w.dest)
      (hbound : c.bound w.dest = true → ∀ q, q ≠ p → ¬ touches c σ q w.dest) :
      Step c σ (injected σ p w b)

/-- Hold points.  Both are named so that they map to graph nodes. -/
inductive Hold where
  /-- The head of slot `p` is a word for destination `d`. -/
  | head (p d : Nat)
  /-- Output `o` is held by slot `p`. -/
  | grant (o p : Nat)
  /-- Transaction `k` has a word in slot `p`, at the head or behind it. -/
  | ctx (k p : Nat)
  /-- Transaction `k` is live with no word anywhere: a target owes it words. -/
  | wait (k : Nat)
  deriving DecidableEq, Repr

/-- The head of a slot is a word for this destination. -/
def headIs (σ : St) (p d : Nat) : Prop := ∃ w, (σ.q p).head? = some w ∧ w.dest = d

/-- Slot `p` has a word of transaction `k`. -/
def hasTok (σ : St) (p k : Nat) : Prop := ∃ w, w ∈ σ.q p ∧ w.tok = k

/-- Transaction `k` is live with no word anywhere. -/
def waiting (σ : St) (k : Nat) : Prop := σ.live k = true ∧ ∀ p, ¬ hasTok σ p k

/-- The holder's head is the word that goes through the output. -/
def headRoutes (c : Config) (σ : St) (p o : Nat) : Prop :=
  ∃ w, (σ.q p).head? = some w ∧ c.route p w.dest = some o

/-- A held output waits for its next word, and the head at `r` is in the way of it:
    at the holder itself, or upstream of a holder that can change destination. -/
def Awaits (c : Config) (σ : St) (r d' o p : Nat) : Prop :=
  Upstream c r p ∧ headIs σ r d' ∧ ¬ headRoutes c σ p o ∧ (r = p ∨ c.cross p = true)

open Classical in
/-- Owner observation. -/
noncomputable def own (σ : St) : Hold → Option Nat
  | .head p d =>
    match (σ.q p).head? with
    | some w => if w.dest = d then some w.tok else none
    | none => none
  | .grant o p => if σ.grant o = some p then some p else none
  | .ctx k p => if hasTok σ p k then some k else none
  | .wait k => if waiting σ k then some k else none

theorem own_ctx_isSome {σ : St} {k p : Nat} :
    (own σ (.ctx k p)).isSome = true ↔ hasTok σ p k := by
  simp only [own]
  by_cases h : hasTok σ p k
  · simp [h]
  · simp [h]

theorem own_ctx_some {σ : St} {k p : Nat} (h : hasTok σ p k) :
    own σ (.ctx k p) = some k := by
  simp [own, h]

theorem own_wait_isSome {σ : St} {k : Nat} :
    (own σ (.wait k)).isSome = true ↔ waiting σ k := by
  simp only [own]
  by_cases h : waiting σ k
  · simp [h]
  · simp [h]

theorem own_wait_some {σ : St} {k : Nat} (h : waiting σ k) :
    own σ (.wait k) = some k := by
  simp [own, h]

theorem own_head_isSome {σ : St} {p d : Nat} :
    (own σ (.head p d)).isSome = true ↔ headIs σ p d := by
  simp only [own, headIs]
  cases h : (σ.q p).head? with
  | none => simp
  | some w =>
    by_cases hd : w.dest = d
    · simp [hd]
    · simp [hd]

theorem own_grant_isSome {σ : St} {o p : Nat} :
    (own σ (.grant o p)).isSome = true ↔ σ.grant o = some p := by
  simp only [own]
  by_cases h : σ.grant o = some p
  · simp [h]
  · simp [h]

/-- Progress: a word left the point. -/
def prog (σ σ' : St) : Hold → Prop
  | .head p d => headIs σ p d ∧ ∃ o, σ'.ev = some (p, o)
  | .grant o p => σ.grant o = some p ∧ σ'.ev = some (p, o)
  | .ctx k p =>
    (∃ w, (σ.q p).head? = some w ∧ w.tok = k) ∧ ∃ o, σ'.ev = some (p, o)
  | .wait k => waiting σ k ∧ ∃ p, hasTok σ' p k

/-- A transaction can add a word: its response slot or its source has room. -/
def CanAdd (c : Config) (σ : St) (k : Nat) : Prop :=
  ∃ r, (σ.home k = some r ∨ σ.src k = some r) ∧ (σ.q r).length < c.cap r

/-- Resource need of a held point. -/
def need (c : Config) (σ : St) : Hold → Hold → Prop
  | .grant o p', .head p d => headIs σ p d ∧ c.route p d = some o ∧ p' ≠ p
  | .head r _, .head p d =>
    headIs σ p d ∧ ∃ o, c.route p d = some o ∧ σ.grant o = some p ∧ blockerOf c o d = some r
  | .head r d', .grant o p =>
    σ.grant o = some p ∧
      ((∃ d, headIs σ p d ∧ c.route p d = some o ∧ blockerOf c o d = some r) ∨
        Awaits c σ r d' o p)
  | .head r d', .ctx k p => r = p ∧ headIs σ p d' ∧ hasTok σ p k
  | .head r _, .wait k => σ.home k = some r ∨ σ.src k = some r
  | _, _ => False

/-- Permission for the corresponding need. -/
def permit (c : Config) (σ : St) : Hold → Hold → Prop
  | .grant o p', .head _ _ => σ.grant o ≠ some p'
  | .head r _, .head p d =>
    ∀ o, c.route p d = some o → σ.grant o = some p → blockerOf c o d = some r → fitsHead c σ p o
  | .head r d', .grant o p =>
    ((∃ d, headIs σ p d ∧ c.route p d = some o ∧ blockerOf c o d = some r) → fitsHead c σ p o) ∧
      ¬ Awaits c σ r d' o p
  | .head r d', .ctx k p =>
    r = p → ∀ w, (σ.q p).head? = some w → w.dest = d' →
      w.tok = k ∧ ∃ o, c.route p d' = some o ∧ σ.grant o = some p ∧ fitsHead c σ p o
  | .head _ _, .wait k => CanAdd c σ k
  | _, _ => True

/-! ### Effects of a fire -/

section Effects
variable {c : Config} {σ : St} {p o : Nat} {w : Word}

theorem place_grant (τ : St) : (place c τ o w).grant = τ.grant := by
  unfold place; split
  · rfl
  · split <;> rfl

theorem place_openOut (τ : St) : (place c τ o w).openOut = τ.openOut := by
  unfold place; split
  · rfl
  · split <;> rfl

theorem place_ev (τ : St) : (place c τ o w).ev = τ.ev := by
  unfold place; split
  · rfl
  · split <;> rfl

theorem place_q_of_next {r : Nat} (τ : St) (h : c.next o = some r) :
    (place c τ o w).q = upd τ.q r (τ.q r ++ [w]) := by
  simp only [place, h]

theorem place_openIn_of_next {r : Nat} (τ : St) (h : c.next o = some r) :
    (place c τ o w).openIn = upd τ.openIn r (nextOpen w) := by
  simp only [place, h]

theorem place_q_of_none (τ : St) (h : c.next o = none) : (place c τ o w).q = τ.q := by
  simp only [place, h]; split <;> rfl

theorem place_openIn_of_none (τ : St) (h : c.next o = none) :
    (place c τ o w).openIn = τ.openIn := by
  simp only [place, h]; split <;> rfl

/-- The queue of a slot after a fire, in one statement. -/
theorem fire_q (hloop : c.next o ≠ some p) (x : Nat) :
    (place c (pop σ p o w) o w).q x =
      if c.next o = some x then σ.q x ++ [w]
      else if x = p then (σ.q p).tail else σ.q x := by
  cases hn : c.next o with
  | none =>
    rw [place_q_of_none _ hn]
    by_cases hx : x = p
    · subst hx; simp [pop]
    · simp [pop, upd_ne, hx]
  | some r =>
    rw [place_q_of_next _ hn]
    by_cases hr : r = x
    · subst hr
      have hrp : r ≠ p := fun e => hloop (by rw [hn, e])
      simp [pop, upd_ne, hrp]
    · have hxr : x ≠ r := fun e => hr e.symm
      rw [upd_ne _ _ hxr]
      by_cases hx : x = p
      · subst hx; simp [pop, hr]
      · simp [pop, upd_ne, hx, hr]

theorem fire_openIn (x : Nat) :
    (place c (pop σ p o w) o w).openIn x =
      if c.next o = some x then nextOpen w else σ.openIn x := by
  cases hn : c.next o with
  | none => rw [place_openIn_of_none _ hn]; simp [pop]
  | some r =>
    rw [place_openIn_of_next _ hn]
    by_cases hr : r = x
    · subst hr; simp
    · have hxr : x ≠ r := fun e => hr e.symm
      rw [upd_ne _ _ hxr]; simp [pop, hr]

theorem fire_openOut (x : Nat) :
    (place c (pop σ p o w) o w).openOut x = if x = p then nextOpen w else σ.openOut x := by
  rw [place_openOut]
  by_cases hx : x = p
  · subst hx; simp [pop]
  · simp [pop, upd_ne, hx]

theorem fire_grant (o' : Nat) :
    (place c (pop σ p o w) o w).grant o' =
      if w.tail = true ∧ o' = o then none else σ.grant o' := by
  rw [place_grant]
  by_cases ht : w.tail = true
  · by_cases ho : o' = o
    · subst ho; simp [pop, ht]
    · simp [pop, ht, ho, upd_ne]
  · simp [pop, ht]

theorem fire_ev : (place c (pop σ p o w) o w).ev = some (p, o) := by
  rw [place_ev]; rfl

/-- Only a fire of a transaction's own word ends it. -/
theorem place_live_other {k : Nat} {τ : St} (hne : w.tok ≠ k) :
    (place c τ o w).live k = τ.live k := by
  unfold place
  split
  · rfl
  · split
    · rfl
    · show (if w.last then upd τ.live w.tok false else τ.live) k = τ.live k
      split
      · exact upd_ne _ _ (fun e => hne e.symm)
      · rfl

end Effects

/-! ### The stream of a slot -/

/-- Each word agrees with the stream state before it. -/
def flowOk : Option Nat → List Word → Prop
  | _, [] => True
  | cur, w :: ws => (∀ d, cur = some d → w.dest = d) ∧ flowOk (nextOpen w) ws

/-- The stream state after a list of words. -/
def flowEnd : Option Nat → List Word → Option Nat
  | cur, [] => cur
  | _, w :: ws => flowEnd (nextOpen w) ws

theorem flowOk_push {cur : Option Nat} {l : List Word} {w : Word}
    (h : flowOk cur l) (hw : ∀ d, flowEnd cur l = some d → w.dest = d) :
    flowOk cur (l ++ [w]) := by
  induction l generalizing cur with
  | nil => exact ⟨hw, trivial⟩
  | cons a as ih => exact ⟨h.1, ih h.2 hw⟩

theorem flowEnd_push {cur : Option Nat} {l : List Word} {w : Word} :
    flowEnd cur (l ++ [w]) = nextOpen w := by
  induction l generalizing cur with
  | nil => rfl
  | cons a as ih => exact ih

theorem head?_push {l : List Word} {a w : Word} (h : l.head? = some a) :
    (l ++ [w]).head? = some a := by
  cases l with
  | nil => simp at h
  | cons x xs => simpa using h

/-! ### Well-formed instances -/

/-- Conditions on the instance tables.  `wfCheck` decides them. -/
structure WF (c : Config) : Prop where
  /-- A coherent endpoint is contiguous, or ends its segments ahead of the next word. -/
  endpoint_contig : ∀ p, p ∈ c.endpoints → c.cross p = false →
    c.contig p = true ∨ ∀ d o, c.route p d = some o → c.ahead p d = true
  /-- The two ways of keeping a stream whole do not mix. -/
  ahead_not_contig : ∀ p d o, c.route p d = some o → c.ahead p d = true → c.contig p = false
  /-- A coherent slot has only coherent feeders. -/
  feed_coherent : ∀ o r p d, c.next o = some r → c.cross r = false →
    c.route p d = some o → c.cross p = false
  /-- Endpoints have no feeder. -/
  endpoint_unfed : ∀ o r, c.next o = some r → r ∉ c.endpoints
  /-- One output feeds a slot. -/
  next_inj : ∀ o o' r, c.next o = some r → c.next o' = some r → o = o'
  /-- A routed destination stays routed. -/
  route_closed : ∀ p d o r, c.route p d = some o → c.next o = some r →
    (c.route r d).isSome = true
  /-- A word that may hold keeps that right downstream. -/
  hold_closed : ∀ p d o r, c.route p d = some o → c.next o = some r →
    (c.mayHold p d = true ∨ c.ahead p d = true) → c.mayHold r d = true
  /-- A bound destination is one step away, on outputs of its own. -/
  bound_direct : ∀ p d o, c.bound d = true → c.route p d = some o → c.next o = none
  bound_own : ∀ p d o p' d', c.bound d = true → c.route p d = some o →
    c.route p' d' = some o → d' = d
  /-- Upstream of a slot that can change destination, the list `ups` is complete. -/
  ups_self : ∀ p d o, c.cross p = true → c.route p d = some o → p ∈ c.ups p
  ups_closed : ∀ p d o r d' o' x, c.cross p = true → c.route p d = some o → x ∈ c.ups p →
    c.route r d' = some o' → c.next o' = some x → r ∈ c.ups p

/-! ### The invariant of legal runs -/

/-- Invariant.  The first three parts hold for every slot; the rest hold for
    slots the instance declares coherent. -/
structure Inv (c : Config) (σ : St) : Prop where
  grant_route : ∀ o p, σ.grant o = some p → ∃ d, c.route p d = some o
  q_route : ∀ p w, w ∈ σ.q p → (c.route p w.dest).isSome = true
  nontail : ∀ p w, w ∈ σ.q p → w.tail = false →
    c.mayHold p w.dest = true ∨ c.ahead p w.dest = true
  held_why : ∀ o p, σ.grant o = some p →
    (∃ w, (σ.q p).head? = some w ∧ c.route p w.dest = some o) ∨
    (∃ d, c.route p d = some o ∧ c.mayHold p d = true)
  /-- An endpoint that decides ahead keeps no such list: only its head counts. -/
  ahead_follow : ∀ p d, c.cross p = false → p ∈ c.endpoints → c.contig p = false →
    σ.openOut p = some d → ∃ w, (σ.q p).head? = some w ∧ w.dest = d
  flow : ∀ p, c.cross p = false → (p ∉ c.endpoints ∨ c.contig p = true) →
    flowOk (σ.openOut p) (σ.q p) ∧ flowEnd (σ.openOut p) (σ.q p) = σ.openIn p
  coh : ∀ o p, c.cross p = false → σ.grant o = some p →
    (∃ w, (σ.q p).head? = some w ∧ σ.openOut p = none ∧ c.route p w.dest = some o) ∨
    (∃ d, σ.openOut p = some d ∧ c.route p d = some o)
  out_grant : ∀ p d, c.cross p = false → σ.openOut p = some d →
    ∃ o, c.route p d = some o ∧ σ.grant o = some p
  feed : ∀ r d, c.cross r = false → r ∉ c.endpoints → σ.openIn r = some d →
    ∃ o p, c.next o = some r ∧ σ.grant o = some p ∧ σ.openOut p = some d
  /-- One slot at a time touches a bound destination. -/
  bound_one : ∀ d p q, c.bound d = true → touches c σ p d → touches c σ q d → p = q

theorem inv_empty (c : Config) : Inv c St.empty where
  grant_route := by intro o p h; simp [St.empty] at h
  q_route := by intro p w h; simp [St.empty] at h
  nontail := by intro p w h; simp [St.empty] at h
  held_why := by intro o p h; simp [St.empty] at h
  ahead_follow := by intro p d _ _ _ h; simp [St.empty] at h
  flow := by intro p _ _; exact ⟨trivial, rfl⟩
  coh := by intro o p _ h; simp [St.empty] at h
  out_grant := by intro p d _ h; simp [St.empty] at h
  feed := by intro r d _ _ h; simp [St.empty] at h
  bound_one := by
    intro d p q _ hp _
    rcases hp with ⟨a, ha, _⟩ | ⟨o, _, hg⟩
    · simp [St.empty] at ha
    · simp [St.empty] at hg

/-- Membership after a fire. -/
theorem mem_fire_q {c : Config} {σ : St} {p o x : Nat} {w a : Word}
    (hloop : c.next o ≠ some p)
    (h : a ∈ (place c (pop σ p o w) o w).q x) :
    a ∈ σ.q x ∨ (a = w ∧ c.next o = some x) := by
  rw [fire_q hloop] at h
  split at h
  · rename_i hn
    rcases List.mem_append.1 h with h | h
    · exact Or.inl h
    · exact Or.inr ⟨by simpa using h, hn⟩
  · split at h
    · rename_i hx
      subst hx
      exact Or.inl (List.mem_of_mem_tail h)
    · exact Or.inl h

/-- The head of a coherent slot agrees with the destination its stream is open toward. -/
theorem follows {c : Config} {σ : St} (I : Inv c σ) {p d : Nat} {w : Word}
    (hc : c.cross p = false) (hd : σ.openOut p = some d) (hh : (σ.q p).head? = some w) :
    w.dest = d := by
  by_cases hor : p ∉ c.endpoints ∨ c.contig p = true
  · have hfl := (I.flow p hc hor).1
    rw [hd] at hfl
    cases hq : σ.q p with
    | nil => rw [hq] at hh; simp at hh
    | cons a as =>
      rw [hq] at hh hfl
      have : a = w := by simpa using hh
      subst this
      exact hfl.1 d rfl
  · have he : p ∈ c.endpoints := by
      apply Classical.byContradiction
      intro h
      exact hor (Or.inl h)
    have hcon : c.contig p = false := by
      cases h : c.contig p with
      | false => rfl
      | true => exact absurd (Or.inr h) hor
    obtain ⟨a, ha, had⟩ := I.ahead_follow p d hc he hcon hd
    rw [hh] at ha
    have : w = a := by simpa using ha
    subst this
    exact had

/-- Ending a segment at the head changes no destination, so nothing else moves. -/
theorem inv_cut {c : Config} (wf : WF c) {σ : St} (I : Inv c σ) {p : Nat} {w : Word}
    (he : p ∈ c.endpoints) (hh : (σ.q p).head? = some w) (hahead : c.ahead p w.dest = true) :
    Inv c { σ with
      q := upd σ.q p ({ w with tail := true } :: (σ.q p).tail)
      ev := none } := by
  obtain ⟨o0, ho0⟩ := Option.isSome_iff_exists.1 (I.q_route p w (List.mem_of_head? hh))
  have hcon : c.contig p = false := wf.ahead_not_contig p w.dest o0 ho0 hahead
  have hmem : ∀ x a, a ∈ upd σ.q p ({ w with tail := true } :: (σ.q p).tail) x →
      a ∈ σ.q x ∨ (a = { w with tail := true } ∧ x = p) := by
    intro x a ha
    by_cases hx : x = p
    · subst hx
      rw [upd_same] at ha
      rcases List.mem_cons.1 ha with h | h
      · exact Or.inr ⟨h, rfl⟩
      · exact Or.inl (List.mem_of_mem_tail h)
    · rw [upd_ne _ _ hx] at ha
      exact Or.inl ha
  have hhd : ∀ x a, (σ.q x).head? = some a →
      ∃ b, (upd σ.q p ({ w with tail := true } :: (σ.q p).tail) x).head? = some b ∧ b.dest = a.dest := by
    intro x a ha
    by_cases hx : x = p
    · subst hx
      rw [hh] at ha
      have : w = a := by simpa using ha
      subst this
      exact ⟨{ w with tail := true }, by rw [upd_same]; rfl, rfl⟩
    · exact ⟨a, by rw [upd_ne _ _ hx]; exact ha, rfl⟩
  refine ⟨I.grant_route, ?_, ?_, ?_, ?_, ?_, ?_, I.out_grant, I.feed, ?_⟩
  · intro x a ha
    rcases hmem x a ha with h | ⟨rfl, rfl⟩
    · exact I.q_route x a h
    · exact I.q_route x w (List.mem_of_head? hh)
  · intro x a ha ht
    rcases hmem x a ha with h | ⟨rfl, _⟩
    · exact I.nontail x a h ht
    · simp at ht
  · intro o' p' hg
    rcases I.held_why o' p' hg with ⟨a, ha, hra⟩ | hright
    · obtain ⟨b, hb, hbd⟩ := hhd p' a ha
      exact Or.inl ⟨b, hb, hbd ▸ hra⟩
    · exact Or.inr hright
  · intro x d hc hx hcn hd
    obtain ⟨a, ha, had⟩ := I.ahead_follow x d hc hx hcn hd
    obtain ⟨b, hb, hbd⟩ := hhd x a ha
    exact ⟨b, hb, hbd.trans had⟩
  · intro x hc hor
    have hx : x ≠ p := by
      intro e
      rcases hor with h | h
      · exact h (e ▸ he)
      · rw [e, hcon] at h; simp at h
    simp only [upd_ne _ _ hx]
    exact I.flow x hc hor
  · intro o' p' hc hg
    rcases I.coh o' p' hc hg with ⟨a, ha, hnone, hra⟩ | hright
    · obtain ⟨b, hb, hbd⟩ := hhd p' a ha
      exact Or.inl ⟨b, hb, hnone, hbd ▸ hra⟩
    · exact Or.inr hright
  · have hback : ∀ d x, touches c { σ with
        q := upd σ.q p ({ w with tail := true } :: (σ.q p).tail)
        ev := none } x d → touches c σ x d := by
      intro d x hx
      rcases hx with ⟨a, ha, had⟩ | hgr
      · rcases hmem x a ha with h | ⟨rfl, rfl⟩
        · exact Or.inl ⟨a, h, had⟩
        · exact Or.inl ⟨w, List.mem_of_head? hh, had⟩
      · exact Or.inr hgr
    intro d x y hb hx hy
    exact I.bound_one d x y hb (hback d x hx) (hback d y hy)

theorem inv_fire {c : Config} (wf : WF c) {σ : St} (I : Inv c σ) {p o : Nat} {w : Word}
    (hh : (σ.q p).head? = some w) (hr : c.route p w.dest = some o)
    (hg : σ.grant o = some p) (hloop : c.next o ≠ some p)
    (hcut : c.ahead p w.dest = true → w.tail = false →
      ∃ a, (σ.q p).tail.head? = some a ∧ a.dest = w.dest) :
    Inv c (place c (pop σ p o w) o w) := by
  have hwmem : w ∈ σ.q p := List.mem_of_head? hh
  -- The head of another slot does not change.
  have hhead : ∀ x a, x ≠ p → (σ.q x).head? = some a →
      ((place c (pop σ p o w) o w).q x).head? = some a := by
    intro x a hx ha
    rw [fire_q hloop]
    split
    · exact head?_push ha
    · exact ha
  -- A coherent firing slot agrees with its open destination.
  have hflowhead : c.cross p = false → ∀ d, σ.openOut p = some d → w.dest = d :=
    fun hc d hd => follows I hc hd hh
  -- The new head of the firing slot, when it has one.
  have hnewhead : ∀ a, (σ.q p).tail.head? = some a →
      ((place c (pop σ p o w) o w).q p).head? = some a := by
    intro a ha
    rw [fire_q hloop, if_neg hloop, if_pos rfl]
    exact ha
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_⟩
  · -- grant_route
    intro o' p' h
    rw [fire_grant] at h
    split at h
    · simp at h
    · exact I.grant_route o' p' h
  · -- q_route
    intro x a ha
    rcases mem_fire_q hloop ha with h | ⟨rfl, hn⟩
    · exact I.q_route x a h
    · exact wf.route_closed p a.dest o x hr hn
  · -- nontail
    intro x a ha ht
    rcases mem_fire_q hloop ha with h | ⟨rfl, hn⟩
    · exact I.nontail x a h ht
    · exact Or.inl (wf.hold_closed p a.dest o x hr hn (I.nontail p a hwmem ht))
  · -- held_why
    intro o' p' h
    rw [fire_grant] at h
    split at h
    · simp at h
    · rename_i hne
      by_cases hp : p' = p
      · subst hp
        by_cases ho : o' = o
        · subst ho
          have ht : w.tail = false := by
            cases htt : w.tail with
            | false => rfl
            | true => exact absurd ⟨htt, rfl⟩ hne
          rcases I.nontail p' w hwmem ht with hm | ha
          · exact Or.inr ⟨w.dest, hr, hm⟩
          · obtain ⟨a, hat, had⟩ := hcut ha ht
            exact Or.inl ⟨a, hnewhead a hat, had ▸ hr⟩
        · rcases I.held_why o' p' h with ⟨a, ha, hra⟩ | hright
          · rw [hh] at ha
            have : w = a := by simpa using ha
            subst this
            rw [hr] at hra
            exact absurd (by simpa using hra.symm) ho
          · exact Or.inr hright
      · rcases I.held_why o' p' h with ⟨a, ha, hra⟩ | hright
        · exact Or.inl ⟨a, hhead p' a hp ha, hra⟩
        · exact Or.inr hright
  · -- ahead_follow
    intro x d hc he hcon hd
    rw [fire_openOut] at hd
    by_cases hx : x = p
    · subst hx
      simp only [if_true, nextOpen] at hd
      split at hd
      · simp at hd
      · rename_i ht
        have hde : w.dest = d := by simpa using hd
        have ht' : w.tail = false := by
          cases htt : w.tail with
          | false => rfl
          | true => exact absurd htt ht
        have hah : c.ahead x w.dest = true := by
          rcases wf.endpoint_contig x he hc with h | h
          · rw [hcon] at h; simp at h
          · exact h w.dest o hr
        obtain ⟨a, hat, had⟩ := hcut hah ht'
        exact ⟨a, hnewhead a hat, had.trans hde⟩
    · simp only [if_neg hx] at hd
      obtain ⟨a, ha, had⟩ := I.ahead_follow x d hc he hcon hd
      exact ⟨a, hhead x a hx ha, had⟩
  · -- flow
    intro x hc hor
    rw [fire_openOut, fire_openIn, fire_q hloop]
    by_cases hn : c.next o = some x
    · -- The slot fed by the output.
      have hxp : x ≠ p := fun e => hloop (by rw [hn, e])
      have hcp : c.cross p = false := wf.feed_coherent o x p w.dest hn hc hr
      simp only [if_pos hn, if_neg hxp]
      obtain ⟨h1, h2⟩ := I.flow x hc hor
      refine ⟨flowOk_push h1 ?_, flowEnd_push⟩
      intro d hd
      rw [h2] at hd
      obtain ⟨o1, p1, hn1, hg1, hop⟩ := I.feed x d hc (wf.endpoint_unfed o x hn) hd
      have ho1 : o1 = o := wf.next_inj o1 o x hn1 hn
      subst ho1
      rw [hg] at hg1
      have : p = p1 := by simpa using hg1
      subst this
      exact hflowhead hcp d hop
    · simp only [if_neg hn]
      by_cases hx : x = p
      · subst hx
        simp only [if_true]
        obtain ⟨h1, h2⟩ := I.flow x hc hor
        cases hq : σ.q x with
        | nil => rw [hq] at hh; simp at hh
        | cons a as =>
          rw [hq] at hh h1 h2
          have : a = w := by simpa using hh
          subst this
          exact ⟨h1.2, h2⟩
      · simp only [if_neg hx]
        exact I.flow x hc hor
  · -- coh
    intro o' p' hc h
    rw [fire_grant] at h
    rw [fire_openOut]
    split at h
    · simp at h
    · rename_i hne
      by_cases hp : p' = p
      · subst hp
        simp only [if_true]
        by_cases ho : o' = o
        · subst ho
          have ht : w.tail = false := by
            cases htt : w.tail with
            | false => rfl
            | true => exact absurd ⟨htt, rfl⟩ hne
          exact Or.inr ⟨w.dest, by simp [nextOpen, ht], hr⟩
        · exfalso
          rcases I.coh o' p' hc h with ⟨a, ha, _, hra⟩ | ⟨d, hd, hrd⟩
          · rw [hh] at ha
            have : w = a := by simpa using ha
            subst this
            rw [hr] at hra
            exact ho (by simpa using hra.symm)
          · have := hflowhead hc d hd
            rw [← this, hr] at hrd
            exact ho (by simpa using hrd.symm)
      · simp only [if_neg hp]
        rcases I.coh o' p' hc h with ⟨a, ha, hnone, hra⟩ | hright
        · exact Or.inl ⟨a, hhead p' a hp ha, hnone, hra⟩
        · exact Or.inr hright
  · -- out_grant
    intro x d hc hd
    rw [fire_openOut] at hd
    by_cases hx : x = p
    · subst hx
      simp only [if_true, nextOpen] at hd
      split at hd
      · simp at hd
      · rename_i ht
        have hde : w.dest = d := by simpa using hd
        refine ⟨o, hde ▸ hr, ?_⟩
        rw [fire_grant, if_neg (fun h => ht h.1)]
        exact hg
    · simp only [if_neg hx] at hd
      obtain ⟨o1, hr1, hg1⟩ := I.out_grant x d hc hd
      refine ⟨o1, hr1, ?_⟩
      rw [fire_grant]
      split
      · rename_i hboth
        rw [hboth.2, hg] at hg1
        exact absurd (by simpa using hg1.symm) hx
      · exact hg1
  · -- feed
    intro r d hc hne hd
    rw [fire_openIn] at hd
    by_cases hn : c.next o = some r
    · simp only [if_pos hn, nextOpen] at hd
      split at hd
      · simp at hd
      · rename_i ht
        have hde : w.dest = d := by simpa using hd
        refine ⟨o, p, hn, ?_, ?_⟩
        · rw [fire_grant, if_neg (fun h => ht h.1)]; exact hg
        · rw [fire_openOut]; simp [nextOpen, ht, hde]
    · simp only [if_neg hn] at hd
      obtain ⟨o1, p1, hn1, hg1, hop⟩ := I.feed r d hc hne hd
      have ho1 : o1 ≠ o := fun e => hn (e ▸ hn1)
      refine ⟨o1, p1, hn1, ?_, ?_⟩
      · rw [fire_grant, if_neg (fun h => ho1 h.2)]; exact hg1
      · rw [fire_openOut]
        by_cases hp1 : p1 = p
        · subst hp1
          exfalso
          obtain ⟨d1, hrd1⟩ := I.grant_route o1 p1 hg1
          have hc1 : c.cross p1 = false := wf.feed_coherent o1 r p1 d1 hn1 hc hrd1
          rcases I.coh o1 p1 hc1 hg1 with ⟨_, _, hnone, _⟩ | ⟨d2, hd2, hrd2⟩
          · rw [hnone] at hop; simp at hop
          · rw [hop] at hd2
            have hdd : d = d2 := by simpa using hd2
            subst hdd
            have := hflowhead hc1 d hop
            rw [← this, hr] at hrd2
            exact ho1 (by simpa using hrd2.symm)
        · simp only [if_neg hp1]; exact hop
  · -- bound_one
    have hback : ∀ d x, c.bound d = true → touches c (place c (pop σ p o w) o w) x d →
        touches c σ x d := by
      intro d x hb hx
      rcases hx with ⟨a, ha, had⟩ | ⟨o1, hr1, hg1⟩
      · rcases mem_fire_q hloop ha with hm | ⟨rfl, hn⟩
        · exact Or.inl ⟨a, hm, had⟩
        · have := wf.bound_direct p d o hb (had ▸ hr)
          rw [this] at hn
          simp at hn
      · rw [fire_grant] at hg1
        split at hg1
        · simp at hg1
        · exact Or.inr ⟨o1, hr1, hg1⟩
    intro d x y hb hx hy
    exact I.bound_one d x y hb (hback d x hb hx) (hback d y hb hy)

theorem inv_arb {c : Config} (wf : WF c) {σ : St} (I : Inv c σ) {p o : Nat} {w : Word}
    (hh : (σ.q p).head? = some w) (hr : c.route p w.dest = some o)
    (hf : σ.grant o = none) :
    Inv c { σ with grant := upd σ.grant o (some p), ev := none } := by
  -- Reading the new grant.
  have hget : ∀ o' p', upd σ.grant o (some p) o' = some p' →
      (o' = o ∧ p' = p) ∨ (o' ≠ o ∧ σ.grant o' = some p') := by
    intro o' p' h
    by_cases ho : o' = o
    · subst ho
      rw [upd_same] at h
      exact Or.inl ⟨rfl, by simpa using h.symm⟩
    · rw [upd_ne _ _ ho] at h
      exact Or.inr ⟨ho, h⟩
  have hkeep : ∀ o' p', σ.grant o' = some p' → upd σ.grant o (some p) o' = some p' := by
    intro o' p' h
    have ho : o' ≠ o := by
      intro e; subst e; rw [hf] at h; simp at h
    rw [upd_ne _ _ ho]; exact h
  refine ⟨?_, I.q_route, I.nontail, ?_, I.ahead_follow, I.flow, ?_, ?_, ?_, ?_⟩
  · intro o' p' h
    rcases hget o' p' h with ⟨rfl, rfl⟩ | ⟨_, h'⟩
    · exact ⟨w.dest, hr⟩
    · exact I.grant_route o' p' h'
  · intro o' p' h
    rcases hget o' p' h with ⟨rfl, rfl⟩ | ⟨_, h'⟩
    · exact Or.inl ⟨w, hh, hr⟩
    · exact I.held_why o' p' h'
  · intro o' p' hc h
    rcases hget o' p' h with ⟨rfl, rfl⟩ | ⟨_, h'⟩
    · refine Or.inl ⟨w, hh, ?_, hr⟩
      cases hop : σ.openOut p' with
      | none => rfl
      | some d =>
        exfalso
        obtain ⟨o1, hr1, hg1⟩ := I.out_grant p' d hc hop
        have hde := follows I hc hop hh
        rw [← hde, hr] at hr1
        have : o' = o1 := by simpa using hr1
        subst this
        rw [hf] at hg1
        simp at hg1
    · exact I.coh o' p' hc h'
  · intro x d hc hd
    obtain ⟨o1, hr1, hg1⟩ := I.out_grant x d hc hd
    exact ⟨o1, hr1, hkeep o1 x hg1⟩
  · intro r d hc hne hd
    obtain ⟨o1, p1, hn1, hg1, hop⟩ := I.feed r d hc hne hd
    exact ⟨o1, p1, hn1, hkeep o1 p1 hg1, hop⟩
  · have hback : ∀ d x, c.bound d = true →
        touches c { σ with grant := upd σ.grant o (some p), ev := none } x d → touches c σ x d := by
      intro d x hb hx
      rcases hx with ⟨a, ha, had⟩ | ⟨o1, hr1, hg1⟩
      · exact Or.inl ⟨a, ha, had⟩
      · rcases hget o1 x hg1 with ⟨rfl, rfl⟩ | ⟨_, h'⟩
        · have := wf.bound_own x d o1 x w.dest hb hr1 hr
          exact Or.inl ⟨w, List.mem_of_head? hh, this⟩
        · exact Or.inr ⟨o1, hr1, h'⟩
    intro d x y hb hx hy
    exact I.bound_one d x y hb (hback d x hb hx) (hback d y hb hy)

theorem inv_inject {c : Config} {σ : St} (I : Inv c σ) {p : Nat} {w : Word}
    (he : p ∈ c.endpoints)
    (hhold : w.tail = true ∨ c.mayHold p w.dest = true ∨ c.ahead p w.dest = true)
    (hcontig : c.contig p = true → ∀ d, σ.openIn p = some d → w.dest = d)
    (hrt : (c.route p w.dest).isSome = true) {b : Nat}
    (hbound : c.bound w.dest = true → ∀ q, q ≠ p → ¬ touches c σ q w.dest) :
    Inv c (injected σ p w b) := by
  have hmem : ∀ x a, a ∈ upd σ.q p (σ.q p ++ [w]) x → a ∈ σ.q x ∨ (a = w ∧ x = p) := by
    intro x a h
    by_cases hx : x = p
    · subst hx
      rw [upd_same] at h
      rcases List.mem_append.1 h with h | h
      · exact Or.inl h
      · exact Or.inr ⟨by simpa using h, rfl⟩
    · rw [upd_ne _ _ hx] at h
      exact Or.inl h
  have hhead : ∀ x a, (σ.q x).head? = some a → (upd σ.q p (σ.q p ++ [w]) x).head? = some a := by
    intro x a h
    by_cases hx : x = p
    · subst hx; rw [upd_same]; exact head?_push h
    · rw [upd_ne _ _ hx]; exact h
  refine ⟨I.grant_route, ?_, ?_, ?_, ?_, ?_, ?_, I.out_grant, ?_, ?_⟩
  · intro x a h
    rcases hmem x a h with h | ⟨rfl, rfl⟩
    · exact I.q_route x a h
    · exact hrt
  · intro x a h ht
    rcases hmem x a h with h | ⟨rfl, rfl⟩
    · exact I.nontail x a h ht
    · rcases hhold with h | h
      · rw [ht] at h; simp at h
      · exact h
  · intro o' p' h
    rcases I.held_why o' p' h with ⟨a, ha, hra⟩ | hright
    · exact Or.inl ⟨a, hhead p' a ha, hra⟩
    · exact Or.inr hright
  · intro x d hc hx hcn hd
    obtain ⟨a, ha, had⟩ := I.ahead_follow x d hc hx hcn hd
    exact ⟨a, hhead x a ha, had⟩
  · intro x hc hor
    by_cases hx : x = p
    · subst hx
      simp only [injected, upd_same]
      obtain ⟨h1, h2⟩ := I.flow x hc hor
      refine ⟨flowOk_push h1 ?_, flowEnd_push⟩
      intro d hd
      rw [h2] at hd
      have hcn : c.contig x = true := by
        rcases hor with h | h
        · exact absurd he h
        · exact h
      exact hcontig hcn d hd
    · simp only [injected, upd_ne _ _ hx]
      exact I.flow x hc hor
  · intro o' p' hc h
    rcases I.coh o' p' hc h with ⟨a, ha, hnone, hra⟩ | hright
    · exact Or.inl ⟨a, hhead p' a ha, hnone, hra⟩
    · exact Or.inr hright
  · intro r d hc hne hd
    have hrp : r ≠ p := fun e => hne (e ▸ he)
    simp only [injected, upd_ne _ _ hrp] at hd
    exact I.feed r d hc hne hd
  · intro d x y hb hx hy
    have hsplit : ∀ z, touches c (injected σ p w b) z d → touches c σ z d ∨ (z = p ∧ w.dest = d) := by
      intro z hz
      rcases hz with ⟨a, ha, had⟩ | hgr
      · rcases hmem z a ha with hm | ⟨rfl, hzp⟩
        · exact Or.inl (Or.inl ⟨a, hm, had⟩)
        · exact Or.inr ⟨hzp, had⟩
      · exact Or.inl (Or.inr hgr)
    rcases hsplit x hx with hx' | ⟨rfl, hd⟩ <;> rcases hsplit y hy with hy' | ⟨hyp, hd'⟩
    · exact I.bound_one d x y hb hx' hy'
    · subst hd'
      subst hyp
      apply Classical.byContradiction
      intro hne
      exact hbound hb x hne hx'
    · subst hd
      apply Classical.byContradiction
      intro hne
      exact hbound hb y (fun e => hne e.symm) hy'
    · exact hyp.symm

theorem inv_step {c : Config} (wf : WF c) {σ σ' : St} (hs : Step c σ σ') (I : Inv c σ) :
    Inv c σ' := by
  cases hs with
  | stutter => exact ⟨I.grant_route, I.q_route, I.nontail, I.held_why, I.ahead_follow, I.flow, I.coh,
      I.out_grant, I.feed, I.bound_one⟩
  | cut p w he hh hahead _ => exact inv_cut wf I he hh hahead
  | arb p o w hh hr hf => exact inv_arb wf I hh hr hf
  | fire p o w hh hr hg _ hloop hcut => exact inv_fire wf I hh hr hg hloop hcut
  | inject p w b he _ hhold hcontig hrt _ _ _ _ _ _ hbound =>
    exact inv_inject I he hhold hcontig hrt hbound

/-- A run of steps from the empty network keeps the invariant. -/
theorem inv_run {c : Config} (wf : WF c) {s : Nat → St} (h0 : s 0 = St.empty)
    (hs : ∀ t, Step c (s t) (s (t + 1))) (t : Nat) : Inv c (s t) := by
  induction t with
  | zero => rw [h0]; exact inv_empty c
  | succ t ih => exact inv_step wf (hs t) ih

/-! ### The central fact about release -/

/-- A held grant whose holder's head goes elsewhere can only exist where the
    instance declared it.  In a coherent slot it cannot occur at all. -/
theorem ups_complete {c : Config} (wf : WF c) {r p d o : Nat} (hc : c.cross p = true)
    (hr : c.route p d = some o) (h : Upstream c r p) : r ∈ c.ups p := by
  induction h with
  | refl => exact wf.ups_self _ d o hc hr
  | step hr1 hn1 _ ih => exact wf.ups_closed _ d o _ _ _ _ hc hr (ih hc hr) hr1 hn1

theorem hold_across_needs_cross {c : Config} {σ : St} (I : Inv c σ) {o p d' : Nat}
    (hg : σ.grant o = some p) (hh : headIs σ p d') (hne : c.route p d' ≠ some o) :
    c.cross p = true ∧ ∃ d, c.route p d = some o ∧ c.mayHold p d = true := by
  obtain ⟨w, hw, rfl⟩ := hh
  have hwhy : ∃ d, c.route p d = some o ∧ c.mayHold p d = true := by
    rcases I.held_why o p hg with ⟨a, ha, hra⟩ | hright
    · rw [hw] at ha
      have : w = a := by simpa using ha
      subst this
      exact absurd hra hne
    · exact hright
  refine ⟨?_, hwhy⟩
  cases hc : c.cross p with
  | true => rfl
  | false =>
    exfalso
    rcases I.coh o p hc hg with ⟨a, ha, _, hra⟩ | ⟨d, hd, hrd⟩
    · rw [hw] at ha
      have : w = a := by simpa using ha
      subst this
      exact hne hra
    · have hde := follows I hc hd hw
      rw [hde] at hne
      exact hne hrd

end Wormhole
