# Pinned PoE Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** 포트 조회/사전 재조회 및 PoE 일괄 제어를 상단 고정 패널에 단계별로 표시한다.

**Architecture:** 독립 PySide6 `PortProgressPanel`을 화면 구성 컴포넌트로 두고 기존 조회/제어 Qt signals에 연결한다. `BatchPortController`에는 읽기 전용 콜백 `on_stage(index, target, phase)`을 추가하며 기존 SSH·변경·감사 경로를 유지한다.

**Tech Stack:** Python 3.14, PySide6, pytest, GitHub Windows Actions.

**Spec:** `docs/superpowers/specs/2026-11-18-pinned-poe-progress-design.md`

## Global Constraints
- main 병합 금지; 별도 브랜치에서 Windows CI 성공 확인 후 사전 릴리스 발행.
- 비밀번호, enable password, running-config 노출 금지.
- 최대 300포트, LLDP/AP-535, SSH 호스트키, 불확실 결과 시 자동 재시도 금지.
- 가상의 테스트 데이터만 사용; 실제 장비 제어 자동화 테스트 금지.

## Review Focus
- 사전 재조회 중에도 현황 헤더가 보이고 실제 명령을 전송했다고 표시하지 않기.
- 이미 ON인 포트만 있으면 변경 작업 0건으로 종료.
- 미확인 포트가 하나라도 있으면 전체 작업이 보류됨을 명확히 표시.
- 실행 중 취소/실패 시 기존 진행 정보가 잘못 성공으로 표시되지 않기.
- 수백 포트 작업 시 GUI 이벤트 루프 블로킹·로그에 계정 비밀번호 누출 금지.

### Task 1: RED tests for panel and stage callback
**Files:** Create `tests/test_port_progress_panel.py`; Modify `tests/test_batch_port_control.py`.
- [ ] 테스트 작성: 조회/사전 재조회, 포트별 단계, 완료/보류, 새 작업 초기화, 콜백 인자 안전성.
- [ ] Windows CI에서 예상되는 RED 결과 확인.

### Task 2: Read-only progress panel
**Files:** Create `src/aruba2930f_backup/port_progress_panel.py`.
- [ ] 고정 헤더, 진행률, 현재 장비/포트, 상세 펼침/접힘, 결과 카운터 구현.
- [ ] 로그/툴팁에 인증정보 미포함 및 bounded row 최대 300 확인.

### Task 3: Stage hooks and GUI integration
**Files:** Modify `src/aruba2930f_backup/batch_port_control.py`, `src/aruba2930f_backup/unified_port_gui.py`, `src/aruba2930f_backup/manager_gui.py`.
- [ ] `BatchPortController.execute(..., on_stage=...)` 옵셔널 이벤트만 추가.
- [ ] Qt worker stage signal과 조회·제어 진행률 연결. fake controller 호환 유지.
- [ ] 조회 버튼 아래에 패널 고정; 기존 PoE ON/OFF 로직·안전 가드 불변.

### Task 4: Documentation, version, release
**Files:** `README.md`, `CHANGELOG.md`, `pyproject.toml`, `build_windows.ps1`, `src/aruba2930f_backup/__init__.py`, `.github/workflows/ci.yml`.
- [ ] 신규 B안 설명/검증을 반영하고 릴리스 버전 동기화.
- [ ] Windows CI 전부 GREEN; ZIP/EXE/SHA256/SBOM 스모크.
- [ ] main 미병합 상태에서 정확히 검증된 브랜치 커밋으로 사전 릴리스만 발행.
