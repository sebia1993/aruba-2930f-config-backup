# Grouped PoE Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver user-approved C: multiple switch-specific port lists, grouped read-only results, reusable target profiles, pasted tables, CSV results.

**Architecture:** Pure target/file contracts feed a bounded read-only SSH collection service. A modal Qt dialog owns editable targets, cancellation, incremental grouped results, profile operations and export. Existing backup and single-port mutation workflows retain their boundaries.

**Tech Stack:** Python 3.14, PySide6, existing pinned Netmiko/Paramiko, stdlib JSON/CSV/concurrency. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-16-grouped-poe-query-design.md`

## Global Constraints

- Target identity is IPv4 + SSH port; each target owns its interfaces. Never form a Cartesian product.
- Up to 100 targets, 384 interfaces per target, 2,000 interfaces total; paste 100,000 characters; profile 1 MiB; label 80 characters.
- Only explicit comma-separated single ports such as `24` and `1/24`; no ranges/all/CX identifiers.
- Maximum 3 concurrent switches; ports sequential in one switch session; connect 15 seconds, command 20 seconds, output 1 MiB/10,000 lines, no automatic retry.
- Unknown/missing/failed/unsupported power is not zero. Actual zero remains zero. Every row retains its own timestamp.
- Profile contains only IP, SSH port, interfaces, label; format `aruba2930f-poe-targets`, version 1. Reject unknown/duplicate keys and bool-as-int.
- CSV is UTF-8 BOM, atomically written, with safe string cells and blank missing watts. UTC ISO 8601 on disk, local display time.
- No real switch access, LLDP additions, recurring polling, energy estimates, credential persistence, batch mutation or new dependencies.
- No mutation of existing backup allowlists or weakening of pinned SSH trust. Existing single-port confirmation remains unchanged.

## Review Focus

- Duplicate endpoint rows with overlapping port lists or conflicting labels must not query the wrong pairing.
- Malformed/oversized/credential-bearing profile and paste inputs must not partially replace the current target set.
- Cancellation while unknown host keys await the GUI must finish without unbounded waits or approving a different request.
- Transport closure during one port must not feed responses into another target or erase results from other switches.
- Refresh subset, parent close and target edits must not expose stale watts or destroy a running Qt thread.

## Shared Interfaces

`poe_query_models.py` defines frozen dataclasses `PoETarget(target: DeviceTarget, interfaces: tuple[str, ...], label: str = "")` and `PoEQueryResult(group: PoETarget, interface: str, status: QueryStatus, snapshot: PortSnapshot | None = None, error_code: ErrorCode | None = None, message: str = "", finished_at: str = "")`. `QueryStatus` is a StrEnum with `OK="ok"`, `UNSUPPORTED="unsupported"`, `FAILED="failed"`, `CANCELLED="cancelled"`.

`poe_targets.py` exports `parse_interfaces(text: str) -> tuple[str, ...]`, `make_target(ip: str, ssh_port: str | int, interfaces: str, label: str = "") -> PoETarget`, `merge_targets(targets: Sequence[PoETarget]) -> tuple[PoETarget, ...]`, `parse_pasted_targets(text: str) -> tuple[PoETarget, ...]`, `save_target_profile(path: Path, targets: Sequence[PoETarget]) -> None`, `load_target_profile(path: Path) -> tuple[PoETarget, ...]`.

`poe_query_report.py` exports `write_query_csv(path: Path, results: Sequence[PoEQueryResult]) -> None`.

`poe_query.py` exports `PoEQueryService(host_key_store: HostKeyStore | None = None, *, host_key_probe: HostKeyProbe | None = None, session_factory: Callable[[DeviceTarget, Credentials, CollectionOptions, HostKeyStore], PortSession] | None = None, max_workers: int = 3)` and `collect(groups: Sequence[PoETarget], credentials: Credentials, *, approve_host_key: Callable[[HostKeyCheck], bool], on_result: Callable[[PoEQueryResult], None] | None = None, cancel_event: threading.Event | None = None) -> tuple[PoEQueryResult, ...]`. Callbacks may originate on pool threads; returned results preserve target/port input order. One terminal result per target-port. Models/files own no credentials.

`poe_query_gui.py` exports `PoEQueryDialog(parent: QWidget | None = None, *, service: PoEQueryServiceProtocol | None = None, initial_ips: Sequence[str] = (), ssh_port: int = 22, username: str = "", password: str = "", enable_password: str = "")` with read-only `is_busy` property. Service protocol matches collect above. Default service construction is lazy; no network at dialog creation.

## Task 1: Targets, profile and export contracts

**Files:** Create `src/aruba2930f_backup/poe_query_models.py`, `poe_targets.py`, `poe_query_report.py`; create `tests/test_poe_targets.py`, `tests/test_poe_query_report.py`.

**Consumes:** Existing DeviceTarget, ErrorCode, PortSnapshot and single port validation.
**Produces:** Shared models and file APIs above. Paste column order is IP, SSH port, interfaces, optional label; TSV or quoted CSV, optional recognized English/Korean header. Merge same endpoint in insertion order; reject conflicting nonempty labels. File failures preserve the old destination.

- [x] Write failing tests for independent IP/port mappings, duplicate endpoints/ports, same IP different SSH port, invalid identifiers and all input bounds; run targeted tests and record expected missing-feature failures.
- [x] Implement model validation, explicit list parser, atomic whole-input paste parsing and merging; rerun tests to green.
- [x] Write and run failing profile roundtrip, unknown/duplicate key, bool port/version, malformed UTF-8/JSON, oversized file and failure-preserves-file tests; implement strict versioned profiles.
- [x] Write and run failing CSV tests for zero vs missing, quoted labels/Unicode/newlines, formula prefixes including leading whitespace, and output failure preservation; implement export.
- [x] Run `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_poe_targets.py tests/test_poe_query_report.py`, Ruff and focused mypy; record results. Root commits after integration to avoid shared index races.

## Task 2: Bounded read-only collection

**Files:** Create `src/aruba2930f_backup/poe_query.py`, `tests/test_poe_query.py`. Any necessary existing helper exposure must be coordinated with root; do not broaden command permissions.

**Consumes:** Shared models/merge function, existing pinned transport, PortSession, identity/interface/PoE parsing.
**Produces:** Service collect API above. No GUI/filesystem side effects except existing trusted-key storage. Use one authenticated session per switch; identity once; refresh port status before each PoE reading so rows' timestamps describe recent observations. Serialize host-key checks/approval/store mutations. Cancellation is checked between bounded operations and by GUI approval callback.

- [x] Write failing behavioral tests with fake network transport and real parsing: independent group mapping, exactly one session per endpoint, only the requested per-port PoE commands, input order and callback delivery. Run and observe expected failure.
- [x] Implement bounded executor and session lifecycle, no mutation or automatic reconnection.
- [x] Add failing tests for per-port malformed output, unsupported PoE vs rejected query, group authentication/identity failure, uncertain transport closure, cancellation before work/mid-switch/approval and parallelism limit; implement their handling.
- [x] Run `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_poe_query.py tests/test_port_control.py tests/test_port_control_ssh.py`, Ruff and focused mypy; record results and owned files for integration.

## Task 3: Qt grouped query workflow

**Files:** Create `src/aruba2930f_backup/poe_query_gui.py` (split owned worker/widgets modules if clarity requires), `tests/test_poe_query_gui.py`; modify `src/aruba2930f_backup/gui.py` for launch/busy/parent-close handling.

**Consumes:** All shared APIs. Service may be injected for offline Qt tests; callback must emit Qt signals, never mutate GUI from pool threads.
**Produces:** Dialog constructor/is_busy above, main `포트 전력 조회…` button. Editable rows use IP/SSH port/interfaces/optional label order, one initial blank row unless inherited IPs. Input is validated/merged before networking. Add/remove/paste/save/load; load asks before replacing nonempty targets, never starts collection. Targets changing clears result state. Common credentials remain in memory only.

- [x] Write failing Qt tests for two IPs with different ports, duplicate merge, grouping by endpoint, watts zero/missing, failed-row display, target edits invalidate results; run and observe expected failure.
- [x] Implement input actions and group tree with per-port rows, local time, details, result progress and error messages.
- [x] Write failing tests for whole/group/port refresh and stale value clearing, unknown-host-key approval cancellation, stop while active, close/parent-close during collection, backup busy entry blocked, credentials absent from profile/export. Implement worker lifecycle and cancel-aware approval.
- [x] Implement save/load/export dialogs with clear error feedback and safe replacement; optional paste dialog uses text, not a spreadsheet engine. Display Ctrl+C/table paste instructions in the product.
- [x] Run Qt tests under `QT_QPA_PLATFORM=offscreen PYTHONPATH=src`, Ruff and mypy; report tests and any integration concerns. Root commits after integration.

## Task 4: Integration, documentation and release

**Files:** User guide `docs/POE_QUERY_KO.md`, README, CHANGELOG, DEVELOPMENT_LOG, validation guide, screenshot renderer/package docs, version declarations/CI/build references and release notes as required for v0.3.0.

- [x] Review task reports and integration contracts; run one full suite with branch coverage, lint/format, mypy, version/secret/package checks required by repository CI. Fix concrete failures with regression evidence.
- [x] Render and inspect actual Qt widgets using fictional data; document explicit list/paste format, profile security, grouped failures, measurement time, cancellation and CSV meaning.
- [x] Fresh independent whole-change review covering spec, safe collection, thread lifecycle and profile/export behavior; address material findings and rerun relevant gates.
- [ ] Upload exact verified tree on feature branch, create PR, wait for Windows/CodeQL/screenshots gates. Complete normal merge/release process consistent with earlier authorization; never bypass protection.
- [ ] Publish v0.3.0 prerelease, download final ZIP/SHA/SBOM and verify version/commit/provenance. Report completion separately from user onsite validation.
