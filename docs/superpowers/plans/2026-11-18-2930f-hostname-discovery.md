# Aruba 2930F Hostname Direct Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, with regression tests before production changes.

**Goal:** Recover switch Hostname reliably from AOS-S system information for PoE/LLDP read and verified PoE changes.

**Architecture:** Shared read-only resolver layered over existing prompt parser and config parser; one extra allowlisted command per switch. Preserve fail-closed network and PoE safety properties.

**Tech Stack:** Python 3.14, PySide6 6.11, Netmiko 4.6, pytest/ruff/mypy, Windows GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-18-2930f-hostname-discovery-design.md`

## Global Constraints
- No change to `main` or prior v0.10.0 Release. Branch-only v0.10.1 prerelease.
- No new dependencies, no arbitrary SSH commands, no automatic write retry.
- One system-information read per switch, fixed exact allowlist.
- Existing SSH host-key trust + model/port check remain authoritative.

## Review Focus
- AOS-S WC.16.01 with incompatible system-info command → prompt/config fallback, no false network failure.
- AOS-S VSF system-info multiple identical `System Name` lines → one valid value, conflicting values rejected.
- ANSI/CRLF/empty/malformed or >255-char names → no junk injected into UI.
- Lost SSH channel/cancellation while reading the system name → error and no hidden unsafe recovery.
- Same switch name before and after PoE change despite different EXEC prompt layout → no false mismatch.

---

### Task 1: Parser and shared resolver
**Files:** `src/aruba2930f_backup/validation.py`, `src/aruba2930f_backup/hostname_discovery.py`, `src/aruba2930f_backup/models.py`; tests `tests/test_validation.py`, `tests/test_hostname_discovery.py`.

- [ ] Add failing unit tests for parsed `System Name`, ambiguous/invalid values and source priority/fallback/cancellation.
- [ ] Run Windows CI to verify correct test failures.
- [ ] Add `hostname_from_system_information(output: str) -> str | None`; add `resolve_switch_hostname(session: HostnameSession, *, before_read: Callable[[], None] | None = None) -> HostnameResolution`; add `DeviceIdentity.hostname_source: str = "unavailable"`.
- [ ] Verify tests and keep any raw running-config out of logs.

### Task 2: Transport and two collection paths
**Files:** `src/aruba2930f_backup/ssh.py`, `src/aruba2930f_backup/poe_query.py`, `src/aruba2930f_backup/port_control.py`, `src/aruba2930f_backup/poe_query_gui.py`; tests `tests/test_ssh.py`, `tests/test_poe_query.py`, `tests/test_port_control.py`.

- [ ] Write failing integration tests: fixed allowlist, system name with non-hostname prompt, once per switch, pre/post PoE name stability; preserve cancel and credentials checks.
- [ ] Allowlist `show system information` and call shared resolver after model check on both collection paths.
- [ ] Show name source in port detail; keep Hostname field and existing PoE verification unchanged.
- [ ] Run full `./tools/validate.ps1 -PythonPath python` in Windows CI, prove failing cases now pass.

### Task 3: Versioned release and proof
**Files:** `pyproject.toml`, `src/aruba2930f_backup/__init__.py`, `build_windows.ps1`, `.github/workflows/ci.yml`, `README.md`, `CHANGELOG.md`, one-shot `.github/workflows/branch-preview-v0101.yml`.

- [ ] Bump aligned version strings to 0.10.1 and document Hostname fix + constraints.
- [ ] Trigger one-shot branch-only guarded release workflow, not main release workflow.
- [ ] Confirm full Windows tests, smoke build, zip hash, SBOM, annotated tag target, public GitHub Release downloadable assets.
- [ ] Notify requested release-email recipient with confirmed asset URL and preserve PR as Draft.
