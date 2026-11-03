-- Must not compile: a graph with a cycle has no rank.
import GenericNoC.EdgeTable
theorem bad : (EdgeTable.mk 3 [(0, 1), (1, 2), (2, 0)] (EdgeTable.rankArr #[0, 1, 2])).checkRank = true := by
  decide +kernel
