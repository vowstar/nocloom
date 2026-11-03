import Lake
open Lake DSL

package «NocLoom» where

@[default_target]
lean_lib «GenericNoC» where
  roots := #[`GenericNoC]
