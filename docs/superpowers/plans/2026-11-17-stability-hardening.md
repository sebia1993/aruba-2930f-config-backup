# Aruba 2930F Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Fix the eight approved failure paths while preserving existing workflows.

**Architecture:** Repair the existing SSH reader, backup service/GUI, host-key store,
and pure input/evidence validators. Use independently owned files for parallel
implementation, serialized controller commits, independent review, and one integrated gate.

**Tech Stack:** Python 3.14, PySide6 6.11, Paramiko/Netmiko, openpyxl, pytest, existing pins.

**Spec:** docs/superpowers/specs/2026-11-17-stability-hardening-design.md

## Global Constraints

- Retain Python 3.14, PySide6, and the existing dependency pins; add no dependency.
- Retain unpublished version 0.3.0, existing target JSON v1/CSV meanings, normal command lists, host-key approval, port-control confirmation, and manual query flow.
- No new actual power operation, automatic reconnect/reboot, or real-device access.
- No arbitrary minimum normal wattage and no blanket Class3-versus-Type2 rejection: PLC and LLDP/DLC can represent different negotiation stages.
- Preserve the existing worktree, previous artifacts, and untracked environment link.
- Changes stay within assigned ownership; controller alone stages/commits/publishes.
- Use .venv/bin/python with absolute PYTHONPATH pointing to this worktree/src and QT_QPA_PLATFORM=offscreen for GUI checks; the editable environment points elsewhere.

## Review Focus

- An idle exact-prompt line inside an unfinished or delayed banner must not silently truncate a successful backup.
- A diagnostic-log error or later host-key failure must preserve already completed configuration files and safe outcome metadata.
- Native cleanup and cancellation-helper cleanup must both finish before the main window permits reuse/destruction.
- Approve and remove must share one cross-instance/process transaction boundary, preserve unrelated keys, and fail closed on lock/store errors.
- Strict LLDP role/Unicode checks must reject malformed values without rejecting valid current output or modifying previous input state.

### Task 1: SSH stream integrity

**Files:** src/aruba2930f_backup/ssh.py, src/aruba2930f_backup/validation.py;
tests/test_ssh.py, tests/test_validation.py, tests/test_collector.py.

**Interfaces:** Preserve NetmikoSSHSession.send_show(command, *, cancel_event=None) -> str
and get_prompt(). If command context is needed in _read_until_prompt, add only an
optional keyword with a default so port-control callers remain compatible.
Keep public validation signatures compatible; private helpers are task-owned.

- [x] Add failing behavioral regressions from transport/reproduce_transport.py: an exact prompt inside a multiline banner followed by multiple idle reads and a delayed interface24; unclosed banner reaching a tail prompt; valid inline/multiline banner forms; split final prompts; pager regression.
- [x] Add a response-partition regression asserting all split positions of a fixed CRLF response yield the same literal normalized text and stored bytes/hash; include trailing CR and empty chunks.
- [x] Run the new tests and record expected RED failures before editing production code.
- [x] Implement response completion with command/body context and lossless streaming normalization. An uncertain/incomplete configuration cannot return successful partial data. Do not solve this merely by increasing idle reads.
- [x] Run pytest tests/test_ssh.py tests/test_validation.py tests/test_collector.py tests/test_loopback_ssh.py tests/test_port_control_ssh.py -q with the shared environment; run focused Ruff/type checks.
- [x] Report and self-review; controller commits, generates the frozen task diff, and obtains task review.

### Task 2: Backup preservation and main GUI failure handling

**Files:** src/aruba2930f_backup/gui.py, src/aruba2930f_backup/reporting.py;
tests/test_gui.py, tests/test_reporting.py. Do not edit hostkeys.py or SSH code.

**Interfaces:** Preserve BackupRequest and the collector protocol. BackupOutcome may
append warnings: tuple[str, ...] = (); ReportSummary may append the same optional
field for durable user-visible warnings. Existing positional constructors remain valid.
Use existing HostKeyStore/service methods; Task3 changes their transactional internals only.

- [x] Add RED regression for one successful collection plus another target retrying: a controlled logger PermissionError at retry_wait must still produce the completed TXT and final workbook, with an explicit warning.
- [x] Add RED regression for a later fatal service/store failure after a prior successful batch: completed configuration files remain saved; primary storage failure is not misreported as success.
- [x] Make diagnostic log OSError a bounded, visible warning instead of abandoning the backup. Persist completed batches or otherwise guarantee their salvage on later failure; preserve paths/digests on repeated final reporting. Never include raw exceptions/secrets in warnings.
- [x] Add RED real-QThread regression for delayed native cleanup: busy and references remain held, GUI timer continues, close is deferred, and completion occurs after cleanup joins. Apply the existing bounded wait/QTimer lifecycle pattern to the main backup window.
- [x] Add RED actual-button tests for malformed host-key listing, failed removal and partial multi-record removal; no uncaught slot exception and a safe actionable message reflects actual store state.
- [x] Implement UI error boundaries without changing trust/approval policy. Handle cancellation helper and QThread cleanup together.
- [x] Run pytest tests/test_gui.py tests/test_reporting.py tests/test_port_control_gui.py tests/test_poe_query_gui.py -q, focused Ruff/type checks, report/self-review; controller commits and obtains review.

### Task 3: Transactional SSH host-key persistence

**Files:** src/aruba2930f_backup/hostkeys.py; optionally a focused internal
src/aruba2930f_backup/hostkey_lock.py; tests/test_hostkeys.py and, if needed,
tests/hostkey_process_worker.py as a test-only child-process helper.

**Interfaces:** Preserve HostKeyStore(path=None), check, require_trusted,
approve(checks), remove(endpoint), list_approved. Lock failure uses existing safe
CollectionFailure/HOST_KEY_REJECTED; callers do not need new arguments.

- [x] Add RED concurrent-instance regression forcing a legal overlap and proving both distinct approvals persist; add approve/remove overlap preserving unrelated records and changed-key rejection.
- [x] Add real child-process contention coverage that runs on Windows spawn and Linux; use bounded coordination/cleanup rather than fragile sleeps. A crashed holder must not permanently lock the store.
- [x] Implement one path-level transaction boundary with standard-library OS locking around read/validation/merge/fsync/replace. Use a finite lock wait and safe failure. The lock file can persist; it is not a stale ownership flag.
- [x] Ensure approve/remove share the transaction; keep damaged stores fail-closed and existing approved entries intact on write errors.
- [x] Run pytest tests/test_hostkeys.py tests/test_loopback_ssh.py -q plus focused Ruff/type checks. Explain Windows-native coverage delegated to CI; report/self-review, controller commit and task review.

### Task 4: LLDP role and Unicode boundary validation

**Files:** src/aruba2930f_backup/poe_power.py, src/aruba2930f_backup/poe_query_models.py,
src/aruba2930f_backup/poe_targets.py (if needed);
tests/test_poe_power.py, tests/test_poe_targets.py, tests/test_poe_query_gui.py.

**Interfaces:** Preserve PowerEvidence/PowerAssessment, existing selected-pair rules,
target profile v1, and atomic UI import. Private role parsing may change; normal
Type2 PD and current manufacturer fixtures remain accepted.

- [x] Add RED regressions: remote Type2 PSE and unrecognized strings containing Type2 never independently establish normal AP supply; recognized PD forms retain normal and insufficient allocation behavior.
- [x] Implement full recognized device-type parsing and source-appropriate role validation. Preserve valid mode evidence from other verified sources and clearly retain malformed/conflicting input as issues. Do not infer insufficiency from class-only or measured watts.
- [x] Add RED profile/target tests for lone high/low surrogate labels: reject before replacing an existing GUI target list/result. Valid Korean and non-BMP labels round-trip unchanged.
- [x] Validate UTF-8/Unicode at the target model/parser boundary so direct API, paste, JSON and UI imports are consistent.
- [x] Run pytest tests/test_poe_power.py tests/test_poe_targets.py tests/test_poe_query_gui.py tests/test_poe_query_report.py -q, focused Ruff/type checks; report/self-review and independent review.

### Task 5: Integration, documentation and delivery

**Files:** README.md/CHANGELOG.md/DEVELOPMENT_LOG.md, relevant usage/validation docs,
this plan/spec as necessary; no dependency/version change. Controller owns this task.

- [x] Resolve reviewed cross-task issues, update accurate user-facing limitations and validation records, and retain per-task RED/GREEN evidence.
- [x] Run the full pytest branch-coverage gate, Ruff check/format, Mypy, version/pin/secrets checks, pip check and the existing dependency-audit policy.
- [x] Freeze the complete diff and obtain one whole-branch review; resolve its actionable findings and scoped re-review before publishing.
- [ ] Update existing PR30 through normal branch controls, verify exact remote/local tree equality, and run required Windows CI.
- [ ] Download the new CI package, verify archive/portable SHA-256, SBOM, build commit/tree and EXE smoke evidence; deliver the actual package and a Korean work/next/progress summary.
