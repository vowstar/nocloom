import NetlistCheck.Check

/-!
# What a passing check means

`check_sound`: when `check verilog netlist` holds, the Verilog text is the
rendering of a module `M`, the netlist text describes a well-formed netlist
`N`, and for every assignment of values to the nets of `M` that satisfies all
of its continuous assignments (`Holds`, with IEEE 1364-2001 expression
meaning), each declared net `nl_internal_v<i>` carries `N.eval` of value `i`,
and each output port carries the value of its driver. `N.eval` reads the
input ports and the leaves (registers, synchronizer outputs, clock gate
outputs) from the same assignment.

`assigns_satisfiable` gives the converse: the netlist's own values satisfy
every assignment of the module, so the module's continuous part is never
contradictory.

So the continuous part of the module computes exactly the netlist's
combinational function. The registers, clock gates and synchronizers are
compared item by item by `conform`; their reading is the fixed meaning of
each construct, not a theorem here.
-/

namespace NetlistCheck
open Netlist

/-! ## Bit-vector facts -/

theorem bv_toNat {w x : Nat} (h : x < 2 ^ w) : (bv w x).toNat = x := by
  simp [bv, BitVec.toNat_ofNat, Nat.mod_eq_of_lt h]

theorem ult_bv {w x y : Nat} (hx : x < 2 ^ w) (hy : y < 2 ^ w) :
    (bv w x).ult (bv w y) = decide (x < y) := by
  simp [bv, BitVec.ult, Nat.mod_eq_of_lt hx, Nat.mod_eq_of_lt hy]

theorem eq_bv {w x y : Nat} (hx : x < 2 ^ w) (hy : y < 2 ^ w) : (bv w x = bv w y) ↔ x = y := by
  constructor
  · intro h
    have := congrArg BitVec.toNat h
    simpa [bv, Nat.mod_eq_of_lt hx, Nat.mod_eq_of_lt hy] using this
  · intro h
    rw [h]

theorem one_bv {x : Nat} (hx : x < 2 ^ 1) : (bv 1 x = 1#1) ↔ x ≠ 0 := by
  rcases x with _ | _ | x <;> simp_all [bv] <;> omega

/-! ## Facts that a passing `conform` provides -/

structure Facts (M : Module) (N : Netlist) (hidden : Array Bool) : Prop where
  wf : N.wf = true
  ports : M.ports.toList = expPorts N
  portsUnique : (N.ports.toList.map fun p => Ident.ofString p.name).Nodup
  portsNamed : ∀ p (h : p < N.ports.size), ∃ s, Ident.ofString N.ports[p].name = .name s
  driversUnique : (N.drivers.toList.map (·.1)).Nodup
  leavesHidden : ∀ i, i < N.values.size → N.isLeafAt i = false → hidden[i]?.getD false = false
  refs : ∀ i, i < N.values.size → N.isLeafAt i = false → ∀ a ∈ N.refs i,
    a < N.values.size ∧ hidden[a]?.getD false = false
  decls : ∀ i, i < N.values.size → hidden[i]?.getD false = false →
    ∃ d ∈ M.decls, d.name = .net i ∧ d.width = N.width i
  declsOnly : ∀ d ∈ M.decls, ∃ i, i < N.values.size ∧ hidden[i]?.getD false = false ∧
    d.name = .net i ∧ d.width = N.width i
  drivers : ∀ pd ∈ N.drivers.toList, pd.2 < N.values.size ∧ hidden[pd.2]?.getD false = false
  assigns : M.assigns = expAssigns N

theorem facts_of_conform {M : Module} {N : Netlist} (h : conform M N = true) :
    ∃ hidden tail, syncCheck M N = .ok (hidden, tail) ∧ Facts M N hidden := by
  unfold conform at h
  split at h
  · rename_i hidden tail hs
    refine ⟨hidden, tail, hs, ?_⟩
    simp only [parts, List.all_cons, List.all_nil, Bool.and_true, Bool.and_eq_true,
      beq_iff_eq] at h
    obtain ⟨hwf, _, hports, _, hpu, hpn, hdu, hleaf, hrefs, hdrv, _, hdecls, hassigns, _, _, _,
      _, _⟩ := h
    refine ⟨hwf, hports, of_decide_eq_true hpu, ?_, of_decide_eq_true hdu, ?_, ?_, ?_, ?_, ?_,
      hassigns⟩
    · intro p hp
      have := Array.all_eq_true.mp hpn p hp
      split at this
      · rename_i s hs
        exact ⟨s, hs⟩
      · simp at this
    · intro i hi hl
      have := List.all_eq_true.mp hleaf i (List.mem_range.mpr hi)
      simpa [hl] using this
    · intro i hi hl a ha
      have := List.all_eq_true.mp hrefs i (List.mem_range.mpr hi)
      simp only [hl, Bool.false_or, List.all_eq_true, Bool.and_eq_true, decide_eq_true_eq,
        Bool.not_eq_true'] at this
      exact this a ha
    · intro i hi hh
      rw [hdecls]
      refine ⟨⟨if N.isStateAt i && !(tail[i]?.getD false) then .reg else .wire, N.width i, .net i⟩,
        List.mem_filterMap.mpr ⟨i, List.mem_range.mpr hi, ?_⟩, rfl, rfl⟩
      simp [hh]
    · intro d hd
      rw [hdecls] at hd
      obtain ⟨i, hi, he⟩ := List.mem_filterMap.mp hd
      by_cases hh : hidden[i]?.getD false = true
      · simp [hh] at he
      · simp only [Bool.not_eq_true] at hh
        simp only [hh] at he
        simp only [Bool.false_eq_true, ↓reduceIte, Option.some.injEq] at he
        exact ⟨i, List.mem_range.mp hi, hh, he ▸ rfl, he ▸ rfl⟩
    · intro pd hpd
      have := List.all_eq_true.mp hdrv pd hpd
      simpa using this
  · simp at h

/-! ## The continuous part computes the netlist -/

section
variable {M : Module} {N : Netlist} {hidden : Array Bool}
  {Γ : Ident → Option Nat} {σ : Ident → Nat}

/-- The module's input port values, by netlist port index. -/
def inpOf (N : Netlist) (σ : Ident → Nat) : Nat → Nat := fun p => σ (N.portIdent p)

/-- The module's net values, by value index. -/
def leafOf (σ : Ident → Nat) : Nat → Nat := fun j => σ (.net j)

theorem width_net (F : Facts M N hidden) (hT : Typed M Γ) {i : Nat} (hi : i < N.values.size)
    (hh : hidden[i]?.getD false = false) : Γ (.net i) = some (N.width i) := by
  obtain ⟨d, hd, hn, hw⟩ := F.decls i hi hh
  have := hT.2 d hd
  rwa [hn, hw] at this

theorem valueOk_of (F : Facts M N hidden) {i : Nat} {v : Value} (hv : N.values[i]? = some v) :
    N.valueOk i v = true := by
  have hwf := F.wf
  simp only [Netlist.wf, Bool.and_eq_true] at hwf
  obtain ⟨⟨⟨⟨_, hvals⟩, _⟩, _⟩, _⟩ := hwf
  obtain ⟨hi, _⟩ := Array.getElem?_eq_some_iff.mp hv
  have := List.all_eq_true.mp hvals i (List.mem_range.mpr hi)
  simpa [hv] using this

theorem assign_of (F : Facts M N hidden) {i : Nat} (hi : i < N.values.size)
    (hl : N.isLeafAt i = false) : (Ident.net i, N.exprOf i) ∈ M.assigns := by
  rw [F.assigns]
  apply List.mem_append_left
  exact List.mem_filterMap.mpr ⟨i, List.mem_range.mpr hi, by simp [hl]⟩

theorem port_width (F : Facts M N hidden) (hT : Typed M Γ) {p : Nat} {port : Port}
    (hp : N.ports[p]? = some port) : Γ (N.portIdent p) = some port.width := by
  have hm : (⟨port.input, port.width, Ident.ofString port.name⟩ : PortDecl) ∈ M.ports.toList := by
    rw [F.ports]
    exact List.mem_map.mpr ⟨port, Array.mem_toList_iff.mpr (Array.mem_of_getElem? hp), rfl⟩
  have := hT.1 _ hm
  simpa [Netlist.portIdent, hp] using this

theorem lt_of_width {x : Ident} {w : Nat} (hR : InRange Γ σ) (h : Γ x = some w) : σ x < 2 ^ w :=
  hR x w h

/-- An input value reads an input port of its width. -/
theorem input_port {N : Netlist} {i p : Nat} {v : Value} (hok : N.valueOk i v = true)
    (hp : v.op = .input p) : ∃ port, N.ports[p]? = some port ∧ port.width = v.width := by
  rcases v with ⟨w, op⟩
  simp only at hp
  subst hp
  simp only [Netlist.valueOk, Op.operands, List.all_nil, Bool.and_true, Bool.and_eq_true] at hok
  cases hport : N.ports[p]? with
  | none => simp [hport] at hok
  | some port =>
    simp only [hport, Bool.and_eq_true, beq_iff_eq] at hok
    exact ⟨port, rfl, hok.2.2⟩

/-- The right side that the module assigns to a value that is not a leaf has
the value's width and the netlist's value, once its operands do. This is the
correspondence of the 14 operations with their Verilog text. -/
theorem ev_exprOf {N : Netlist} {Γ : Ident → Option Nat} {σ : Ident → Nat}
    {inp leaf : Nat → Nat} {i : Nat} {v : Value}
    (hv : N.values[i]? = some v) (hok : N.valueOk i v = true) (hl : v.op.isLeaf = false)
    (hin : ∀ p, v.op = .input p →
      Γ (N.portIdent p) = some v.width ∧ σ (N.portIdent p) = inp p ∧ inp p < 2 ^ v.width)
    (hops : ∀ a ∈ N.refs i, Γ (.net a) = some (N.width a) ∧ σ (.net a) = N.eval inp leaf a ∧
      σ (.net a) < 2 ^ N.width a) :
    ev Γ σ (N.exprOf i) = some (v.width, N.eval inp leaf i) := by
  have hwi : N.width i = v.width := by simp [Netlist.width, hv]
  rw [Netlist.eval, hv]
  rcases v with ⟨w, op⟩
  simp only at hwi hl hin hok ⊢
  cases hop : op <;> simp only [hop, Op.isLeaf, Bool.true_eq_false] at hl hin
  all_goals
    simp only [Netlist.refs, Netlist.op?, hv, hop, Option.map_some, Op.operands,
      List.mem_cons, List.mem_nil_iff, or_false, forall_eq_or_imp, forall_eq] at hops
    simp only [Netlist.exprOf, Netlist.op?, hv, hop, Option.map_some, hwi]
    simp only [Netlist.valueOk, hop, Op.operands, List.all_cons, List.all_nil,
      Bool.and_true, Bool.and_eq_true, decide_eq_true_eq, beq_iff_eq, and_assoc] at hok
  case input p =>
    obtain ⟨hΓ, hσ, hlt⟩ := hin p rfl
    simp [ev, hΓ, hσ, bv_toNat hlt]
  case const l =>
    obtain ⟨hw0, _, hlt⟩ := hok
    simp [ev, bv_toNat hlt, hlt, hw0]
  case zext a =>
    obtain ⟨_, _, hai, _, hwa⟩ := hok
    obtain ⟨hΓa, hσa, hlta⟩ := hops
    rw [dif_pos hai, ← hσa, BitVec.toNat_setWidth, bv_toNat hlta,
      Nat.mod_eq_of_lt (Nat.lt_of_lt_of_le hlta (Nat.pow_le_pow_right (by omega) (by omega)))]
    simp only [ev, hΓa, Option.map_some]
    rw [if_pos ⟨by omega, Nat.two_pow_pos _⟩]
    simp only [Nat.zero_shiftLeft, Nat.zero_or, Option.some.injEq, Prod.mk.injEq, and_true]
    omega
  case ult a b =>
    obtain ⟨_, _, hai, hbi, _, _, hab, hw1⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [← hab] at hΓb hltb
    simp only [dif_pos hai, dif_pos hbi, ← hσa, ← hσb, ult_bv hlta hltb]
    simp [ev, hΓa, hΓb, BinOp.apply, hw1]
  case eq a b =>
    obtain ⟨_, _, hai, hbi, _, _, hab, hw1⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [← hab] at hΓb hltb
    simp only [dif_pos hai, dif_pos hbi, ← hσa, ← hσb]
    simp only [ev, hΓa, hΓb, Option.map_some, ↓reduceIte, BinOp.apply, hw1]
    by_cases h : σ (.net a) = σ (.net b)
    · simp [h]
    · simp [h, mt (eq_bv hlta hltb).mp h]
  case not a =>
    obtain ⟨_, _, hai, _, hwa⟩ := hok
    obtain ⟨hΓa, hσa, hlta⟩ := hops
    rw [hwa] at hΓa hlta
    rw [dif_pos hai, ← hσa, BitVec.toNat_not, bv_toNat hlta]
    simp [ev, hΓa]
  case and a b =>
    obtain ⟨_, _, hai, hbi, _, _, hwa, hwb⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [hwa] at hΓa hlta
    rw [hwb] at hΓb hltb
    rw [dif_pos hai, dif_pos hbi, ← hσa, ← hσb, BitVec.toNat_and, bv_toNat hlta, bv_toNat hltb]
    simp [ev, hΓa, hΓb, BinOp.apply]
  case or a b =>
    obtain ⟨_, _, hai, hbi, _, _, hwa, hwb⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [hwa] at hΓa hlta
    rw [hwb] at hΓb hltb
    rw [dif_pos hai, dif_pos hbi, ← hσa, ← hσb, BitVec.toNat_or, bv_toNat hlta, bv_toNat hltb]
    simp [ev, hΓa, hΓb, BinOp.apply]
  case sub a b =>
    obtain ⟨_, _, hai, hbi, _, _, hwa, hwb⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [hwa] at hΓa hlta
    rw [hwb] at hΓb hltb
    rw [dif_pos hai, dif_pos hbi, ← hσa, ← hσb, BitVec.toNat_sub, bv_toNat hlta, bv_toNat hltb]
    simp [ev, hΓa, hΓb, BinOp.apply]
  case select c a b =>
    obtain ⟨_, _, hci, hai, hbi, _, _, _, hwc, hwa, hwb⟩ := hok
    obtain ⟨⟨hΓc, hσc, hltc⟩, ⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [hwc] at hΓc hltc
    rw [hwa] at hΓa hlta
    rw [hwb] at hΓb hltb
    rw [dif_pos hci, dif_pos hai, dif_pos hbi, ← hσc, ← hσa, ← hσb, bv_toNat hlta, bv_toNat hltb]
    simp only [ev, hΓc, hΓa, hΓb, Option.map_some, and_self, ↓reduceIte]
    by_cases h : σ (.net c) = 0
    · simp [h, bv]
    · simp [h, (one_bv hltc).mpr h]
  case slice a o =>
    obtain ⟨hw0, _, hai, _, _, _, _, hfit⟩ := hok
    obtain ⟨hΓa, hσa, hlta⟩ := hops
    rw [dif_pos hai, ← hσa, BitVec.extractLsb'_toNat, bv_toNat hlta]
    by_cases h1 : N.width a = 1
    · rw [h1] at hlta hfit
      have ho : N.lit o = 0 := by omega
      simp only [h1, beq_self_eq_true, ↓reduceIte, ev, hΓa, Option.map_some, ho,
        Nat.shiftRight_zero, Option.some.injEq, Prod.mk.injEq]
      have hw1 : w = 1 := by omega
      subst hw1
      exact ⟨rfl, by rw [Nat.mod_eq_of_lt hlta]⟩
    · have hne : (N.width a == 1) = false := by simpa using h1
      simp only [hne, Bool.false_eq_true, ↓reduceIte, ev, hΓa]
      rw [if_pos (by omega)]
      have hw : N.lit o + w - 1 - N.lit o + 1 = w := by omega
      simp only [hw]
  case concat a b =>
    obtain ⟨_, _, hai, hbi, _, _, hw⟩ := hok
    obtain ⟨⟨hΓa, hσa, hlta⟩, hΓb, hσb, hltb⟩ := hops
    rw [dif_pos hai, dif_pos hbi, ← hσa, ← hσb, BitVec.toNat_append, bv_toNat hlta, bv_toNat hltb]
    simp [ev, hΓa, hΓb, hw]

/-- Every declared value net carries the netlist's value. -/
theorem eval_eq (F : Facts M N hidden) (hT : Typed M Γ) (hR : InRange Γ σ) (hH : Holds M Γ σ) :
    ∀ i, i < N.values.size → hidden[i]?.getD false = false →
      σ (.net i) = N.eval (inpOf N σ) (leafOf σ) i := by
  intro i
  induction i using Nat.strongRecOn with
  | ind i ih =>
  intro hi hh
  have hv : N.values[i]? = some N.values[i] := Array.getElem?_eq_getElem hi
  generalize N.values[i] = v at hv
  have hok := valueOk_of F hv
  have hΓi := width_net F hT hi hh
  have hwi : N.width i = v.width := by simp [Netlist.width, hv]
  rw [hwi] at hΓi
  have hσi := lt_of_width hR hΓi
  cases hleaf : v.op.isLeaf
  · have hl : N.isLeafAt i = false := by simp [Netlist.isLeafAt, Netlist.op?, hv, hleaf]
    obtain ⟨w', hw', hev⟩ := hH _ _ (assign_of F hi hl)
    rw [hΓi] at hw'
    cases hw'
    have hok' : ∀ a ∈ v.op.operands, a < i := by
      have h := hok
      simp only [Netlist.valueOk, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at h
      exact h.1.1.2
    have hrefs : ∀ a ∈ N.refs i, a < i := by
      intro a ha
      apply hok' a
      rcases v with ⟨w, op⟩
      cases op <;> simp_all [Netlist.refs, Netlist.op?, Op.operands]
    have hin : ∀ p, v.op = .input p → Γ (N.portIdent p) = some v.width ∧
        σ (N.portIdent p) = inpOf N σ p ∧ inpOf N σ p < 2 ^ v.width := by
      intro p hp
      obtain ⟨port, hport, hpw⟩ := input_port hok hp
      have hΓp := port_width F hT hport
      rw [hpw] at hΓp
      exact ⟨hΓp, rfl, lt_of_width hR hΓp⟩
    have hops : ∀ a ∈ N.refs i, Γ (.net a) = some (N.width a) ∧
        σ (.net a) = N.eval (inpOf N σ) (leafOf σ) a ∧ σ (.net a) < 2 ^ N.width a := by
      intro a ha
      obtain ⟨han, hah⟩ := F.refs i hi hl a ha
      have hΓa := width_net F hT han hah
      exact ⟨hΓa, ih a (hrefs a ha) han hah, lt_of_width hR hΓa⟩
    rw [ev_exprOf hv hok hleaf hin hops] at hev
    simp only [Option.some.injEq, Prod.mk.injEq] at hev
    exact hev.2.symm
  · rw [Netlist.eval, hv]
    rcases v with ⟨w, op⟩
    simp only at hσi ⊢
    cases op <;> simp_all [bv_toNat hσi, leafOf, Op.isLeaf]

/-- Each output port carries the value of its driver. -/
theorem driver_eq (F : Facts M N hidden) (hT : Typed M Γ) (hR : InRange Γ σ) (hH : Holds M Γ σ) :
    ∀ pd ∈ N.drivers.toList, σ (N.portIdent pd.1) = N.eval (inpOf N σ) (leafOf σ) pd.2 := by
  intro pd hpd
  have hm : (N.portIdent pd.1, Expr.id (.net pd.2)) ∈ M.assigns := by
    rw [F.assigns]
    exact List.mem_append_right _ (List.mem_map.mpr ⟨pd, hpd, rfl⟩)
  obtain ⟨w, hw, hev⟩ := hH _ _ hm
  obtain ⟨hd, hh⟩ := F.drivers pd hpd
  have hΓd := width_net F hT hd hh
  simp only [ev, hΓd, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hev
  rw [← hev.2]
  exact eval_eq F hT hR hH pd.2 hd hh

end

/-! ## The continuous part is satisfiable -/

theorem driverOk_of {N : Netlist} (hwf : N.wf = true) {pd : Nat × Nat} (h : pd ∈ N.drivers.toList) :
    N.driverOk pd = true := by
  simp only [Netlist.wf, Bool.and_eq_true] at hwf
  obtain ⟨k, hk, he⟩ := List.mem_iff_getElem.mp h
  simp only [Array.length_toList] at hk
  have := Array.all_eq_true.mp hwf.1.2 k hk
  rwa [← Array.getElem_toList, he] at this

theorem valueOk_wf {N : Netlist} (hwf : N.wf = true) {i : Nat} {v : Value}
    (hv : N.values[i]? = some v) : N.valueOk i v = true := by
  simp only [Netlist.wf, Bool.and_eq_true] at hwf
  obtain ⟨⟨⟨⟨_, hvals⟩, _⟩, _⟩, _⟩ := hwf
  obtain ⟨hi, _⟩ := Array.getElem?_eq_some_iff.mp hv
  have := List.all_eq_true.mp hvals i (List.mem_range.mpr hi)
  simpa [hv] using this

theorem ite_lt {p : Prop} [Decidable p] {a b n : Nat} (ha : a < n) (hb : b < n) :
    (if p then a else b) < n := by
  split <;> assumption

/-- Each value of the netlist fits its width. -/
theorem eval_lt {N : Netlist} (hwf : N.wf = true) (inp leaf : Nat → Nat) {i : Nat}
    (hi : i < N.values.size) : N.eval inp leaf i < 2 ^ N.width i := by
  have hv : N.values[i]? = some N.values[i] := Array.getElem?_eq_getElem hi
  generalize N.values[i] = v at hv
  have hok := valueOk_wf hwf hv
  have hwi : N.width i = v.width := by simp [Netlist.width, hv]
  rw [hwi, Netlist.eval, hv]
  rcases v with ⟨w, op⟩
  cases op
  all_goals dsimp only
  case ult a b | eq a b =>
    have hw1 : w = 1 := by
      simp only [Netlist.valueOk, Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq] at hok
      exact hok.2.2
    subst hw1
    exact ite_lt (by decide) (by decide)
  case select c a b => exact ite_lt (BitVec.isLt _) (BitVec.isLt _)
  case concat a b =>
    have hw : w = N.width a + N.width b := by
      simp only [Netlist.valueOk, Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq] at hok
      exact hok.2
    rw [hw]
    exact BitVec.isLt _
  all_goals exact BitVec.isLt _

/-- The port whose name is `x`, if any. -/
noncomputable def portAt (N : Netlist) (x : Ident) : Option Nat :=
  if h : ∃ p, p < N.ports.size ∧ N.portIdent p = x then some (Classical.choose h) else none

theorem portAt_port {N : Netlist}
    (hu : (N.ports.toList.map fun p => Ident.ofString p.name).Nodup) {p : Nat}
    (hp : p < N.ports.size) : portAt N (N.portIdent p) = some p := by
  have hex : ∃ q, q < N.ports.size ∧ N.portIdent q = N.portIdent p := ⟨p, hp, rfl⟩
  simp only [portAt, hex, ↓reduceDIte, Option.some.injEq]
  obtain ⟨hq, he⟩ := Classical.choose_spec hex
  generalize Classical.choose hex = q at hq he
  have hpair := List.pairwise_iff_getElem.mp hu
  simp only [List.length_map, Array.length_toList, List.getElem_map, Array.getElem_toList] at hpair
  have hname : ∀ k (hk : k < N.ports.size), N.portIdent k = Ident.ofString N.ports[k].name := by
    intro k hk
    simp [Netlist.portIdent, Array.getElem?_eq_getElem hk]
  rw [hname q hq, hname p hp] at he
  rcases Nat.lt_trichotomy q p with h | h | h
  · exact absurd he (hpair q p hq hp h)
  · exact h
  · exact absurd he.symm (hpair p q hp hq h)

theorem portAt_net {N : Netlist}
    (hn : ∀ p (h : p < N.ports.size), ∃ s, Ident.ofString N.ports[p].name = .name s) (i : Nat) :
    portAt N (.net i) = none := by
  simp only [portAt, dite_eq_right_iff, reduceCtorEq, imp_false, not_exists, not_and]
  intro p hp he
  obtain ⟨s, hs⟩ := hn p hp
  simp [Netlist.portIdent, Array.getElem?_eq_getElem hp, hs] at he

section
variable {M : Module} {N : Netlist} {hidden : Array Bool}

open Classical in
/-- The value an output port's driver gives it. -/
noncomputable def drv (N : Netlist) (inp leaf : Nat → Nat) (p : Nat) : Nat :=
  if h : ∃ d, (p, d) ∈ N.drivers.toList then N.eval inp leaf (Classical.choose h) else 0

theorem drv_eq (F : Facts M N hidden) {inp leaf : Nat → Nat} {p d : Nat}
    (h : (p, d) ∈ N.drivers.toList) : drv N inp leaf p = N.eval inp leaf d := by
  have hex : ∃ d, (p, d) ∈ N.drivers.toList := ⟨d, h⟩
  rw [drv, dif_pos hex]
  have hs := Classical.choose_spec hex
  generalize Classical.choose hex = e at hs
  -- Two drivers of one port are the same driver.
  have hu := F.driversUnique
  obtain ⟨j, hj, hje⟩ := List.mem_iff_getElem.mp h
  obtain ⟨k, hk, hke⟩ := List.mem_iff_getElem.mp hs
  have hpair := List.pairwise_iff_getElem.mp hu
  simp only [List.length_map, List.getElem_map] at hpair
  rcases Nat.lt_trichotomy j k with hjk | hjk | hjk
  · exact absurd (by rw [hje, hke]) (hpair j k hj hk hjk)
  · subst hjk
    rw [hje] at hke
    cases hke
    rfl
  · exact absurd (by rw [hje, hke]) (hpair k j hk hj hjk)

/-- Widths of the canonical assignment: ports first, then declared value nets. -/
noncomputable def Γ0 (N : Netlist) (hidden : Array Bool) (x : Ident) : Option Nat :=
  match portAt N x with
  | some p => some ((N.ports[p]?.map (·.width)).getD 0)
  | none =>
    match x with
    | .net i => if i < N.values.size ∧ hidden[i]?.getD false = false then some (N.width i) else none
    | .name _ => none

/-- The canonical assignment: inputs, driven outputs and netlist values. -/
noncomputable def σ0 (N : Netlist) (inp leaf : Nat → Nat) (x : Ident) : Nat :=
  match portAt N x with
  | some p => if (N.ports[p]?.map (·.input)).getD false then inp p else drv N inp leaf p
  | none =>
    match x with
    | .net i => N.eval inp leaf i
    | .name _ => 0

/-- The netlist's own values satisfy every continuous assignment of the module,
for any inputs and leaves in range, and keep those inputs and leaves. -/
theorem assigns_satisfiable (F : Facts M N hidden) (inp leaf : Nat → Nat)
    (hinp : ∀ p (h : p < N.ports.size), N.ports[p].input → inp p < 2 ^ N.ports[p].width)
    (hleaf : ∀ i, N.isLeafAt i = true → leaf i < 2 ^ N.width i) :
    Typed M (Γ0 N hidden) ∧ InRange (Γ0 N hidden) (σ0 N inp leaf) ∧
      Holds M (Γ0 N hidden) (σ0 N inp leaf) ∧
      (∀ p (h : p < N.ports.size), N.ports[p].input → σ0 N inp leaf (N.portIdent p) = inp p) ∧
      (∀ i, i < N.values.size → N.isLeafAt i = true → σ0 N inp leaf (.net i) = leaf i) := by
  have hwf := F.wf
  have hport : ∀ p (h : p < N.ports.size), Γ0 N hidden (N.portIdent p) = some N.ports[p].width ∧
      σ0 N inp leaf (N.portIdent p) =
        if N.ports[p].input then inp p else drv N inp leaf p := by
    intro p hp
    simp [Γ0, σ0, portAt_port F.portsUnique hp, Array.getElem?_eq_getElem hp]
  have hnet : ∀ i, i < N.values.size → hidden[i]?.getD false = false →
      Γ0 N hidden (.net i) = some (N.width i) ∧ σ0 N inp leaf (.net i) = N.eval inp leaf i := by
    intro i hi hh
    simp [Γ0, σ0, portAt_net F.portsNamed, hi, hh]
  refine ⟨⟨?_, ?_⟩, ?_, ?_, ?_, ?_⟩
  · -- Ports have their declared widths.
    intro pd hpd
    rw [F.ports] at hpd
    obtain ⟨port, hmem, hpe⟩ := List.mem_map.mp hpd
    obtain ⟨p, hp, hpp⟩ := List.mem_iff_getElem.mp hmem
    simp only [Array.length_toList] at hp
    rw [Array.getElem_toList] at hpp
    subst hpp hpe
    have := (hport p hp).1
    simpa [Netlist.portIdent, Array.getElem?_eq_getElem hp] using this
  · -- Declared nets have their widths.
    intro d hd
    obtain ⟨i, hi, hh, hn, hw⟩ := F.declsOnly d hd
    rw [hn, hw]
    exact (hnet i hi hh).1
  · -- Every net holds a value of its width.
    intro x w hx
    cases hp : portAt N x with
    | some p =>
      have hpx := hp
      simp only [portAt] at hpx
      split at hpx
      · rename_i hex
        simp only [Option.some.injEq] at hpx
        obtain ⟨hpl, hpe⟩ := Classical.choose_spec hex
        rw [hpx] at hpl hpe
        subst hpe
        obtain ⟨hΓ, hσ⟩ := hport p hpl
        rw [hΓ, Option.some.injEq] at hx
        subst hx
        rw [hσ]
        split
        · rename_i hin
          exact hinp p hpl hin
        · rename_i hout
          unfold drv
          split
          · rename_i hex'
            have hs := Classical.choose_spec hex'
            have hok := driverOk_of hwf hs
            simp only [Netlist.driverOk, Array.getElem?_eq_getElem hpl, Bool.and_eq_true,
              decide_eq_true_eq, Bool.not_eq_true', beq_iff_eq] at hok
            rw [hok.2.2]
            exact eval_lt hwf inp leaf hok.1
          · exact Nat.two_pow_pos _
      · simp at hpx
    | none =>
      cases x with
      | net i =>
        simp only [Γ0, hp] at hx
        split at hx
        · rename_i hc
          simp only [Option.some.injEq] at hx
          subst hx
          simp only [σ0, hp]
          exact eval_lt hwf inp leaf hc.1
        · simp at hx
      | name s => simp [Γ0, hp] at hx
  · -- Every continuous assignment holds.
    intro x e hm
    rw [F.assigns] at hm
    rcases List.mem_append.mp hm with hm | hm
    · obtain ⟨i, hi, he⟩ := List.mem_filterMap.mp hm
      have hi := List.mem_range.mp hi
      by_cases hl : N.isLeafAt i = true
      · simp [hl] at he
      · simp only [Bool.not_eq_true] at hl
        simp only [hl, Bool.false_eq_true, ↓reduceIte, Option.some.injEq, Prod.mk.injEq] at he
        obtain ⟨rfl, rfl⟩ := he
        have hh := F.leavesHidden i hi hl
        obtain ⟨hΓi, hσi⟩ := hnet i hi hh
        refine ⟨N.width i, hΓi, ?_⟩
        have hv : N.values[i]? = some N.values[i] := Array.getElem?_eq_getElem hi
        generalize N.values[i] = v at hv
        have hwi : N.width i = v.width := by simp [Netlist.width, hv]
        have hleafv : v.op.isLeaf = false := by
          simpa [Netlist.isLeafAt, Netlist.op?, hv] using hl
        rw [hσi, hwi]
        apply ev_exprOf hv (valueOk_wf hwf hv) hleafv
        · intro p hp
          obtain ⟨port, hpo, hpw⟩ := input_port (valueOk_wf hwf hv) hp
          have hok := valueOk_wf hwf hv
          rcases v with ⟨w, op⟩
          simp only at hp hpw ⊢
          subst hp
          simp only [Netlist.valueOk, Op.operands, List.all_nil, Bool.and_true, hpo,
            Bool.and_eq_true, beq_iff_eq] at hok
          obtain ⟨hpl, hpe⟩ := Array.getElem?_eq_some_iff.mp hpo
          subst hpe
          obtain ⟨hΓ, hσ⟩ := hport p hpl
          rw [hσ, if_pos hok.2.1, ← hpw]
          exact ⟨hΓ, rfl, hinp p hpl hok.2.1⟩
        · intro a ha
          obtain ⟨han, hah⟩ := F.refs i hi hl a ha
          obtain ⟨hΓa, hσa⟩ := hnet a han hah
          exact ⟨hΓa, hσa, hσa ▸ eval_lt hwf inp leaf han⟩
    · obtain ⟨pd, hpd, he⟩ := List.mem_map.mp hm
      simp only [Prod.mk.injEq] at he
      obtain ⟨rfl, rfl⟩ := he
      have hok := driverOk_of hwf hpd
      simp only [Netlist.driverOk, Bool.and_eq_true, decide_eq_true_eq] at hok
      obtain ⟨hd, hpo⟩ := hok
      cases hpp : N.ports[pd.1]? with
      | none => simp [hpp] at hpo
      | some port =>
        simp only [hpp, Bool.and_eq_true, Bool.not_eq_true', beq_iff_eq] at hpo
        obtain ⟨hpl, hpe⟩ := Array.getElem?_eq_some_iff.mp hpp
        obtain ⟨hdd, hdh⟩ := F.drivers pd hpd
        obtain ⟨hΓd, hσd⟩ := hnet pd.2 hdd hdh
        obtain ⟨hΓp, hσp⟩ := hport pd.1 hpl
        refine ⟨_, hΓp, ?_⟩
        rw [hpe] at hσp ⊢
        rw [hσp, if_neg (by simp [hpo.1]), drv_eq F (by simpa using hpd)]
        simp [ev, hΓd, hσd, hpo.2]
  · intro p hp hin
    rw [(hport p hp).2, if_pos hin]
  · intro i hi hl
    have hv : N.values[i]? = some N.values[i] := Array.getElem?_eq_getElem hi
    have hlt := hleaf i hl
    simp only [σ0, portAt_net F.portsNamed]
    rw [Netlist.eval, hv]
    have hwi : N.width i = N.values[i].width := by simp [Netlist.width, hv]
    rw [hwi] at hlt
    generalize N.values[i] = v at hv hlt
    rcases v with ⟨w, op⟩
    simp only [Netlist.isLeafAt, Netlist.op?, hv, Option.map_some, Option.any_some] at hl
    cases op <;> simp only [Op.isLeaf, Bool.false_eq_true] at hl <;> exact bv_toNat hlt

end

/-- What a passing `check` proves. The Verilog text renders back from its
parse `M`, and the netlist text gives a well-formed netlist `N` that `M`
conforms to. Under any values of the nets of `M` that satisfy all of its
continuous assignments, every declared value net and every output port
carries the netlist's combinational value of the module's own inputs and
leaves. Conversely, for any input and leaf values in range, the netlist's
values satisfy every continuous assignment of `M`. -/
theorem check_sound {verilog netlist : String} (h : check verilog netlist = true) :
    ∃ M N, parse verilog = .ok M ∧ M.render = verilog ∧ parseNetlist netlist = .ok N ∧
      N.wf = true ∧ conform M N = true ∧
      (∀ (Γ : Ident → Option Nat) (σ : Ident → Nat), Typed M Γ → InRange Γ σ → Holds M Γ σ →
        (∀ i, (∃ d ∈ M.decls, d.name = .net i) →
          σ (.net i) = N.eval (inpOf N σ) (leafOf σ) i) ∧
        (∀ pd ∈ N.drivers.toList, σ (N.portIdent pd.1) = N.eval (inpOf N σ) (leafOf σ) pd.2)) ∧
      (∀ inp leaf : Nat → Nat,
        (∀ p (h : p < N.ports.size), N.ports[p].input → inp p < 2 ^ N.ports[p].width) →
        (∀ i, N.isLeafAt i = true → leaf i < 2 ^ N.width i) →
        ∃ (Γ : Ident → Option Nat) (σ : Ident → Nat), Typed M Γ ∧ InRange Γ σ ∧ Holds M Γ σ ∧
          (∀ p (h : p < N.ports.size), N.ports[p].input → σ (N.portIdent p) = inp p) ∧
          (∀ i, i < N.values.size → N.isLeafAt i = true → σ (.net i) = leaf i)) := by
  unfold check at h
  split at h
  · rename_i M N hM hN
    simp only [Bool.and_eq_true, beq_iff_eq] at h
    obtain ⟨hr, hc⟩ := h
    obtain ⟨hidden, _, _, F⟩ := facts_of_conform hc
    refine ⟨M, N, hM, hr, hN, F.wf, hc, fun Γ σ hT hR hH => ⟨?_, driver_eq F hT hR hH⟩,
      fun inp leaf hinp hleaf => ⟨_, _, assigns_satisfiable F inp leaf hinp hleaf⟩⟩
    intro i ⟨d, hd, hn⟩
    obtain ⟨j, hj, hh, hjn, _⟩ := F.declsOnly d hd
    rw [hn] at hjn
    cases hjn
    exact eval_eq F hT hR hH i hj hh
  · simp at h

end NetlistCheck
