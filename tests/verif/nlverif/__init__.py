"""Unified transaction-level verification environment for NocLoom RTL.

SV side (tests/verif/sv): task-free clocked BFMs with online protocol and
contract-timing monitors, a tape interpreter for closed-loop stimulus, and a
JSON Lines event log. Python side: bench generation, an independent offline
model and checker, a negative-control harness, and one driver (run.py).
Verdicts come only from exit codes and results.json.
"""

SCHEMA = 1
