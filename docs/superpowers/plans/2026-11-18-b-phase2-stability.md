# B안 2차 안정화 실행 계획 (2026-11-18)

**Spec**: `docs/superpowers/specs/2026-11-18-b-phase2-stability-design.md`
**Base**: PR #47 `ff436a1b2cde5c06933f2250a53413bb7c14b41c`
**Branch**: `fix/v0.8.1-b-phase2-20261118`
**Hard gate**: main/Release 미수정, 변경 명령 현장 실행 금지.

### Task 1: PR #46 변경 통합
- 파일: `src/aruba2930f_backup/{poe_targets.py,reporting.py}`, `tests/{test_poe_targets.py,test_reporting.py}`.
- PR #46 테스트 통과 SHA `f7d645feb43a42cc54239c1491c4d66d163925ed`를 기준으로 4개 파일의 변경을 동일하게 반영.
- 비교: PR #47과 직접 수정 파일 중복 없음을 `compare`로 확인.
- 검증: 기존 테스트 통과, XLSX fsync 오류 시 이전 결과 보존, JSONL 모든 문자열 필드 마스킹, 300대 포트 병합 상한·중복 유지.

### Task 2: LLDP 캐시와 변경 전 실패 분류
- 파일: `batch_port_control.py`, `port_control.py`, `tests/test_batch_port_control.py`, `tests/test_port_control.py`, `tests/test_unified_port_gui.py`, `tests/test_poe_cycle.py`.
- RED: cached AP-535 LLDP + link Down 상태를 초기에 허용하는 회귀 테스트; 재검증 중 link Down·이웃 없음·다른 Chassis/Port·LLDP 거절을 변경 전 실패로 분류하는 테스트; 전송 후 timeout은 unverified 그대로.
- GREEN: 일괄 OFF/Down 계획 + 실제 변경 직전 둘 다 link Up 요구. typed prewrite failure subclass를 도입하여 변경 전이면 preflight_failed, 불확실 전송은 unverified. crash intent=unknown을 보존.
- 예상: 단일 포트 제어와 PoE ON 경로는 영향 없음, LINK Up으로 LLDP freshness를 증명한다는 주장은 금지.

### Task 3: 300대×1~2포트 GUI 합성 성능
- 파일: `poe_query_gui.py`, `tests/test_poe_query_gui.py`, `tests/test_unified_port_gui.py`.
- RED: 100/200/300 스위치×2포트의 동기 `_on_result`가 큐를 사용하지 않아 과도하게 렌더링됨을 입증. 30대는 정상 즉시 갱신.
- GREEN: 스위치 그룹 수 기반 대규모 렌더링 진입; `_update_group` 배치당 1회; 기존 체크 상태/작업 선택·누락 결과 보존.
- 벤치마크는 지연 절대값 강제보다 관측/호출 횟수와 결과 정확성을 검증. 실제 네트워크 처리량은 주장의 대상 아님.

### Task 4: 문서·전체 검증
- 파일: `docs/SSH_AND_SAFETY.md`, `docs/PORT_OPERATOR_FLOW_KO.md`, `docs/DEPENDENCY_AUDIT_EXCEPTIONS_KO.md`, 위 신규 설계/계획.
- 검증: Windows CI의 Ruff/format/mypy/pytest+coverage/pip-audit/Windows 빌드 및 스모크가 success인지 GitHub Actions에서 확인.
- 리뷰: PR #46/#45/#47과 코드·정책 중복 검토, 자체 최종 코드 리뷰 (subagent 도구 없음 명시).
- 산출: 기존 PR #47을 base로 하는 stacked Draft PR #49 생성. 검증 데이터·현장검증 미수행 범위 기록, 기존 PR 보존, `main` 미병합·미릴리스.


### 검증 기록 및 병합 주의사항
- 수정 전 RED Windows CI #44190588436: 의도한 7개 안전·성능 회귀 테스트가 실패했고 기존 테스트 1,080개 통과. 원인 확인 후 수정.
- 최종 코드 Windows CI [#44192091018](https://github.com/sebia1993/aruba-2930f-config-backup/actions/runs/44192091018): Python 3.14.10, **1,087 passed / 1 skipped**, 총 커버리지 **86.68%**, Ruff/mypy/기존 감사 예외 포함 의존성 검사 및 Windows 포터블 EXE 빌드/스모크/ZIP 검증 통과.
- GitHub PR #48(별도 병렬 작업)은 PR #49와 일부 파일(최초 비교 시 12개)이 겹침. PR #46 내용도 양쪽에 포함됨. 따라서 PR #48과 #49를 순서 없이 모두 main에 병합하지 않고 변경별 중복·안전성/테스트를 대조한 후 통합 필요.
- 본 PR의 Base는 PR #47 개발 브랜치이고, PR #45/#46/#47/#48 원본 브랜치는 변경하지 않음. main 및 릴리스는 사용자 승인 전 유지.
- 자동 검증은 실제 Aruba 2930F의 LLDP TTL/PoE/현장 서비스 검증을 대체하지 않음. Link Up도 LLDP 수신 최신성을 보증하지 않음.
