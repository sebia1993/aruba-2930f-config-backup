# Aruba 2930F Stability Hardening

## Intent and authorization

The user approved the eight-item diagnosis and improvement sequence with “진행해.”
This document records that existing scope for implementation; it does not introduce
another approval gate. The deliverable is an updated PR30 and a verified Windows
portable CI build. Real switch/AP validation remains outside this work.

## Required behavior

1. An exact prompt inside a configuration banner must never make a truncated backup
   succeed. Preserve complete configuration text across arbitrary SSH chunking and
   idle reads; reject an incomplete/ambiguous banner rather than save a success.
2. CRLF split across chunks must produce identical normalized configuration bytes
   and SHA-256 to the same unfragmented response.
3. Diagnostic log I/O failure must not discard successful configuration results.
   Show a safe warning, preserve completed backups during later failures, and keep
   primary file/report errors visible. Do not print credentials or raw exceptions.
4. Main backup GUI must retain worker/thread and busy state until native thread
   cleanup has joined; the event loop must remain responsive during cleanup.
5. Host-key listing/removal failures must show a safe Korean diagnostic, keep trust
   checks intact, and represent actual remaining records after partial removal.
6. Concurrent HostKeyStore instances/processes must not lose approve/remove updates.
   Protect read/validate/merge/replace transactions with bounded process-safe
   locking; never automatically accept a changed host key or reset a damaged file.
7. LLDP device type must use a recognized complete grammar and the correct remote
   PD role. Type2 PSE and unrecognized strings containing Type2 cannot independently
   establish AP-535 normal supply. Preserve legitimate existing power evidence.
8. Invalid Unicode scalar values in target labels must be rejected before UI state
   replacement; Korean and valid non-BMP characters must round-trip unchanged.

## Constraints

- Retain Python 3.14, PySide6, and the existing dependency pins; add no dependency.
- Retain unpublished version 0.3.0, existing target JSON v1/CSV meanings, normal
  command lists, host-key approval, port-control confirmation, and manual query flow.
- No new actual power operation, automatic reconnect/reboot, or real-device access.
- No arbitrary minimum normal wattage and no blanket Class3-versus-Type2 rejection:
  PLC and LLDP/DLC can represent different negotiation stages.
- Preserve the existing worktree, previous artifacts, and untracked environment link.
- Changes stay within assigned ownership; controller alone stages/commits/publishes.
- Use .venv/bin/python with absolute PYTHONPATH pointing to this worktree/src and
  QT_QPA_PLATFORM=offscreen for GUI checks; the editable environment points elsewhere.

## Approach and decisions

Apply focused fixes in existing components with failure-first behavioral tests,
then independent task reviews and one whole-branch review. This implements the
approved improvement sequence. Broad refactoring or new data storage frameworks
would expand scope; arbitrary timing delays would not prove response completeness.

The controller chooses standard-library file locks, a backward-compatible warning
field for backup outcomes/reports, and existing bounded join/timer patterns.
Operational logs may degrade with a warning; primary configuration/report failures
must remain distinguishable. API additions must be optional and preserve callers.

## Evidence and acceptance

The prior audit lives under artifacts/stability-review-2026-11-17. The six principal
failure paths were reproduced, including real writer bytes and Qt event handling.
The LLDP role and invalid-Unicode cases are synthetic malformed inputs, not claims
of live AP-535 output. Acceptance requires red-to-green regression evidence, the
full existing suite and standard static/audit gates, exact publication tree matching,
Windows tests/build/smoke, and verified downloaded ZIP/SBOM/build provenance.
