-- Must not compile: a table with same-slot hold edges does not cover a holder whose
-- next word is upstream.  This is the multi-hop hang.
import GenericNoC.Instances.MultiHop
open Wormhole Wormhole.MultiHop
theorem bad : checkRule cfg ix #[] nodeOf .holdAcross = true := by decide +kernel
