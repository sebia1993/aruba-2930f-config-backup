# Port Control Implementation Plan

> **For agentic workers:** Use the spec and independent code review; tests must use synthetic devices.

**Goal:** SW IP와 인터페이스로 단일 포트의 PoE/통신을 끄고 켠다.

**Architecture:** 백업 세션의 읽기 전용 경계를 유지한 별도 제어 서비스/SSH 어댑터와 PySide6 창.

**Tech Stack:** 기존 Python 3.14, PySide6, Netmiko/Paramiko.

**Spec:** `docs/superpowers/specs/2026-11-16-port-control-design.md`

## Global Constraints

단일 대상, 명시적 대상 확인, 지문/모델 확인, 재조회, 자동 변경 재시도 금지,
자동 저장 금지, 운영 장비 테스트 금지, 비밀번호 저장 금지.

## Review Focus

- 잘못된 포트 문자열이나 조회한 대상 변경이 다른 포트를 제어하지 않는가.
- PoE 미지원/해석 불가 상태에서 PoE 명령을 보내지 않는가.
- SSH 단절 후 실제 수행 여부를 실패/성공으로 단정하거나 재전송하지 않는가.
- Enabled와 물리 링크, Power Enable과 실제 공급 상태가 구분되는가.
- 창 닫기/연속 클릭/동시 백업이 실행 중인 작업이나 스레드를 손상시키지 않는가.

## Tasks

- [x] 서비스·파서·SSH 어댑터: `port_control.py`, `port_control_ssh.py` 및 해당 테스트.
  잘못된 입력/지문/모델/사전 확인/불명확한 결과를 먼저 테스트하고 구현한다.
- [x] GUI: `port_control_gui.py`, 기존 `gui.py`의 진입 버튼, 오프라인 GUI 테스트.
  조회 완료 전 변경 불가, 입력 변경 시 승인 무효화, 실행 확인 및 스레드 수명을 확인한다.
- [x] README·개발/SSH/구조 문서·개발 로그를 실제 기능 범위와 일치시킨다.
- [x] 기존 전체 테스트, Ruff, Mypy, 비밀정보 검사와 독립 리뷰를 수행한다.
- [ ] 기능 브랜치를 GitHub에 올리고 검토 가능한 PR로 전달한다.

## Decisions

- 별도 격리 체크아웃/기능 브랜치에서 작업한다. 사용자 요청으로 기능 추가가
  승인된 범위이며, 실장비 명령 실행·기본 브랜치 병합·릴리즈 발행은 수행하지 않는다.
- 실장비 출력 차이는 명확히 검출하여 변경 전에 중단한다. 추측으로 파싱하지 않는다.
