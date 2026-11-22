import Lake
open Lake DSL

package «NocLoom» where

@[default_target]
lean_lib «GenericNoC» where
  roots := #[`GenericNoC]

-- Generated RTL read back and compared with its netlist (NetlistCheck/Sound.lean).
lean_lib «NetlistCheck» where
  roots := #[`NetlistCheck]

lean_exe «netlist_check» where
  root := `NetlistCheck.Main
