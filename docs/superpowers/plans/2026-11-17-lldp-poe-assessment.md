# LLDP and AP-535 Power Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Implement the user's approved two-column grouped PoE/LLDP view with switch hostnames and manufacturer-grounded AP-535 supply assessment.

**Architecture:** Keep the existing pinned, bounded per-endpoint session and existing mutation contracts intact. Add a pure evidence/parser/assessment module, independent LLDP observations on query results, and progressive PoE-first collection. Consumers render/export the same immutable results.

**Tech Stack:** Python 3.14, PySide6, Netmiko/Paramiko, pytest, Ruff, Mypy; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-17-lldp-poe-assessment-design.md`

## Global Constraints

The Spec's Global Constraints bind every task. Use `.venv/bin/python` and absolute `PYTHONPATH=/workspace/scratch/96ae0be36791/aruba-2930f-poe-groups/src`; the editable environment points at the other checkout. `QT_QPA_PLATFORM=offscreen` is required for GUI checks. No worker commits, pushes, mutates another worker's owned files, or starts child agents; the controller performs sequential commits and independent reviews. Meaningful RED→GREEN tests precede changed behavior; no prose/constant snapshot tests.

## Review Focus

- Partial PoE results must survive later LLDP failure, cancellation, and worker failure fallback.
- An ambiguous/zero/mismatched negotiation value or model must never produce an unqualified normal assessment.
- Normal two-column input must not drop existing nondefault SSH metadata or silently collapse endpoints.
- AP data must bind to the exact local port and fresh query; arbitrary device text must stay inert.
- Hostname and completion progress must survive all-per-port failures and incremental/duplicate callbacks.

## Frozen interfaces

Task 1 owns `poe_power.py`, with immutable dataclasses (all optional fields below default empty/None) and enums:

- `LLDPStatus`: `PENDING`, `OK`, `FAILED`, `CANCELLED` (lowercase values).
- `AssessmentStatus`: `NORMAL`, `WARNING`, `OFF`, `SEARCHING`, `ERROR`, `UNKNOWN`, `UNSUPPORTED` (lowercase values).
- `PowerEvidence`: `requested_watts: Decimal|None`, `allocated_watts: Decimal|None`, `power_class: int|None`, `plc_class: int|None`, `plc_type: int|None`, `dlc_class: int|None`, `dlc_type: int|None`, `allocation_method: str`, `device_type: str`, `allocation_field: str`, `issues: tuple[str,...]`.
- `LLDPNeighbor`: `system_name`, `system_description`, `model`, `chassis_type`, `chassis_id`, `port_type`, `port_id`, `port_description` (str); `management_addresses: tuple[str,...]`; `power: PowerEvidence|None`.
- `LLDPObservation`: `status: LLDPStatus=LLDPStatus.PENDING`, `neighbors: tuple[LLDPNeighbor,...]=()`, `error_code: ErrorCode|None=None`, `message: str=''`, `observed_at: str=''`.
- `PowerAssessment`: required `status: AssessmentStatus`, `label: str`, `reason: str`; optional `model: str='AP-535'`, `model_source: str='configured'`, `requested_watts: Decimal|None`, `allocated_watts: Decimal|None`, `negotiation: str=''`, `reference_id: str` defaulting to the V22 reference ID.
- `AP535_REFERENCE_ID = 'HPE-530-QuickSpecs-V22-2026-08-17'` and `AP535_REFERENCE_URL` official QuickSpecs URL.
- `parse_power_evidence(output: str, interface: str) -> PowerEvidence`.
- `parse_lldp_neighbors(output: str, interface: str) -> tuple[LLDPNeighbor,...]`.
- `assess_ap535_power(poe: PoEState|None, evidence: PowerEvidence|None, lldp: LLDPObservation|None, *, expected_model: str|None='AP-535') -> PowerAssessment`.

Task 2 appends backward-compatible optional fields to `PoEQueryResult`: `identity: DeviceIdentity|None=None`, `power_evidence: PowerEvidence|None=None`, `lldp: LLDPObservation|None=None`, `assessment: PowerAssessment|None=None`, `poe_finished_at: str=''`, `complete: bool=True`. Existing `status` remains the PoE outcome. `finished_at` is the record completion time. `complete=False` means another update is due and is never counted as terminal.

The SSH adapter adds `read_lldp(interface: str) -> str`, generating only `show lldp info remote-device <validated-single-port>`. A query-specific protocol may extend PortSession; do not widen the generic backup command allowlist or mutation methods.

### Task 1: Evidence, LLDP parsing, and power assessment

**Files:** Create `src/aruba2930f_backup/poe_power.py`, `tests/test_poe_power.py`, and uniquely named `tests/fixtures/lldp_*`/`power_evidence_*` fixtures.

**Interfaces:** Produces all frozen pure interfaces above. Consumes existing PoEState, ErrorCode, CollectionFailure, and interface validation.

- [x] Add failing behavioral tests with hand-derived expected results for requested25.5/allocated25.5/actual8.7, requested25.5/allocated20, missing/zero/negative/nonfinite numbers, current faults/off/searching, Class4-only, dot3 with MED disabled, conflicting evidence, AP535 description vs friendly-name-only vs other model, and the configured-model provenance.
- [x] Add failing parser tests for complete manufacturer-shaped fictional outputs, empty recognized LLDP output, missing optional name/address, multiple neighbors, VSF exact port matching, command rejection, unexpected text, duplicate/conflicting numeric fields, output bounds and label/unit variations. Never parse arbitrary failure text as no neighbors.
- [x] Implement the pure contracts. Preserve ambiguity rather than guessing a healthy result; no W-based normal lower threshold.
- [x] Run `.venv/bin/python -m pytest tests/test_poe_power.py -q` with the shared environment; expect all tests pass. Run focused Ruff/Mypy for owned code and report RED/GREEN evidence.

### Task 2: Collection and SSH boundary

**Files:** Modify `poe_query_models.py`, `poe_query.py`, `port_control_ssh.py`, `tests/test_poe_query.py`, `tests/test_port_control_ssh.py`; all under existing src/test directories. Do not modify Task1 or Task3 files.

**Interfaces:** Consumes Task1 contracts, produces appended query result fields and `read_lldp` method. One result per port in final return; callbacks may give an incomplete then complete record.

- [x] Write and observe failing tests for PoE-first order per endpoint, progressive updates, no extra session, exact read-only command generation, all-PoE-fail hostname retention, LLDP success despite unsupported PoE, LLDP failure/cancel/transport loss retaining earlier measurements, and terminal results for all requested ports.
- [x] Collect raw PoE output once and parse state plus optional evidence. Preserve already-known identity on success and failure. Retain existing bounded transport and host-key validation.
- [x] Complete the first PoE pass, then LLDP pass. Normal CLI errors are per-port; transport uncertainty ends the endpoint. Finalize pending observations without erasing completed work.
- [x] Run `.venv/bin/python -m pytest tests/test_poe_query.py tests/test_port_control_ssh.py tests/test_port_control.py -q`; expect pass. Re-run with Task1 tests when its module is available.

### Task 3: Input compatibility and desktop UI

**Files:** Modify `poe_targets.py`, `poe_query_gui.py`, `tests/test_poe_targets.py`, `tests/test_poe_query_gui.py` only.

**Interfaces:** Consumes Task2's result fields and Task1 observation/assessment dataclasses. Existing JSON v1 remains unchanged. Two-column paste uses SSH22; existing three/four-column paste keeps old interpretation.

- [x] Write and observe failing tests for two-column TSV/quoted CSV and headers, legacy roundtrip with2222 and label, atomic rejection, and same IP/different SSH ports.
- [x] Keep stored SSH/label cells or equivalent metadata intact; show only IP and interface columns by default. Add an advanced toggle, automatically revealing nondefault inherited/imported ports. Keep aliases in details/group metadata. Update paste help.
- [x] Write and observe failing GUI tests for IP+hostname even if all ports fail, APname/없음 vs error, per-port watts without aggregates, assessment+reason, incomplete callback progress, duplicate callbacks, partial-result fallback, and refresh clearing both PoE/LLDP values.
- [x] Render concise main columns (`스위치 / 포트`, `AP 이름 (LLDP)`, `현재 전력`, `PoE 판정`, `조회 시각 (로컬)`, `조회 상태`). Put link, PoE settings, model provenance, request/allocation, rule reference and independent phase times into plain-text details. Keep thread/host-key/cancellation lifecycle behavior intact.
- [x] Run `.venv/bin/python -m pytest tests/test_poe_targets.py tests/test_poe_query_gui.py -q`; expect pass. No changes to main-window control behavior.

### Task 4: CSV, documentation, integration and delivery

**Files:** Controller owns `poe_query_report.py`, `tests/test_poe_query_report.py`, docs/README/changelog/log/validation, `tools/render_docs_screenshots.py` and its tests if required.

**Interfaces:** Consume final contracts above. Append fields to CSV, preserving prior column meanings. One row per endpoint+port; LLDP neighbors can be serialized in a JSON cell with safe spreadsheet text handling.

- [x] Add failing CSV tests for independent statuses/times, hostname without snapshot, actualzero vs missing, Decimal request/allocation, rule/model provenance, partial measurements surviving LLDP failure, multiple neighbors without watts duplication and untrusted strings.
- [x] Implement CSV append fields and update human-facing docs with official sources, limits, simple input examples and the precise meaning of normal. Update actual Qt screenshot fixtures and render source-bound screenshots.
- [x] Run full pytest/coverage, Ruff, Mypy, version/dependency/safety checks. Expected all applicable checks pass; report any environmental skip explicitly.
- [x] Whole-branch review and one consolidated final fix wave. Task-scoped reviews, parser/native lifecycle follow-ups and final schema fix re-review passed; final local suite829passed/1PowerShellskip,87.33%coverage.
- [ ] Push the validated feature update and update PR #30, verify remote tree equality, Windows CI, and downloadable portable package. Existing0.3.0 is unreleased, so keep its version while updating the feature branch.
