# 포트폴리오 검토 안내: 신뢰할 수 있는 설정 백업

이 프로젝트에서 검토할 역량은 **SSH 수집의 성공 조건을 정의하고, 일부 장비 실패를 격리하며, 저장 결과를 검증하는 운영 자동화 설계**입니다. 아래 경로는 기능 설명을 구현과 회귀 테스트에 연결합니다.

## 코드로 확인하는 설계 판단

| 검토 질문 | 구현 근거 | 재현 근거 |
|---|---|---|
| 잘못된 모델이나 페이징 실패에서 설정을 읽지 않는가? | [collector.py](../src/aruba2930f_backup/collector.py), [validation.py](../src/aruba2930f_backup/validation.py) | [test_collector.py](../tests/test_collector.py)의 `test_no_page_failure_prevents_every_show_command`, `test_unsupported_model_is_blocked_before_running_config` |
| 인증 실패와 일시적인 timeout을 다르게 처리하는가? | [collector.py](../src/aruba2930f_backup/collector.py)의 제한된 재시도 라운드 | 같은 테스트 파일의 `test_non_transient_authentication_failure_is_not_retried`, `test_collect_many_defers_retry_until_every_target_finishes_current_round` |
| 처음 승인한 SSH 키가 바뀌면 멈추는가? | [hostkeys.py](../src/aruba2930f_backup/hostkeys.py), [ssh.py](../src/aruba2930f_backup/ssh.py) | [test_hostkeys.py](../tests/test_hostkeys.py)의 `test_changed_key_fails_closed_and_cannot_be_reapproved_until_removed` |
| 백업 파일이 중간에 실패하면 완성된 파일로 보이지 않는가? | [storage.py](../src/aruba2930f_backup/storage.py), [reporting.py](../src/aruba2930f_backup/reporting.py) | [test_storage.py](../tests/test_storage.py)의 `test_atomic_write_failure_removes_partial_file` |

명령 순서는 `test_collection_enforces_exact_command_order_and_normalizes_hash`에서 직접 확인할 수 있습니다. `no page` 이후의 `terminal width 511` 세션 준비, 모델 확인, 설정 수집, 최종 프롬프트 확인을 포함합니다. SHA-256은 **저장한 바이트의 무결성**을 확인하는 근거이며, 수동 장비 출력과 내용이 일치한다는 별도의 증거는 아닙니다.

## 장비 없이 재현하기

Windows PowerShell과 Python 3.14에서 저장소 루트 기준으로 실행합니다. 아래 테스트는 [ScriptedSession / ScriptedFactory](../tests/fakes.py)의 합성 응답을 사용합니다. 실제 장비 주소나 계정 입력이 필요하지 않습니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pytest -q tests/test_collector.py tests/test_storage.py
```

살펴볼 결과는 정상 수집 한 건의 설정 해시 일치, 페이징·미지원 모델에서 수집 차단, 인증 실패의 무재시도, 임시 파일 정리입니다. 테스트의 `passed`는 이 합성 조건의 동작만 의미합니다. 전체 정적 검사·커버리지·감사는 [개발 가이드](../DEVELOPMENT.md)의 `tools/validate.ps1`로 이어집니다.

## 검증 기록을 읽는 순서

1. [변경 커밋](https://github.com/sebia1993/aruba-2930f-config-backup/commits/main/)에서 검토한 소스 SHA를 확인합니다.
2. [Windows CI 실행](https://github.com/sebia1993/aruba-2930f-config-backup/actions/workflows/ci.yml)에서 해당 SHA의 완료 결과를 확인합니다. [워크플로 정의](../.github/workflows/ci.yml)에는 Python 3.14, 테스트·검사, 문서 화면 생성, Windows 패키지 검증이 연결되어 있습니다.
3. 바이너리를 평가한다면 [Releases](https://github.com/sebia1993/aruba-2930f-config-backup/releases)의 태그·ZIP·SHA-256·SBOM과 소스 SHA를 대조합니다. 현재 `main`의 테스트 성공이 과거 바이너리를 다시 검증한 결과는 아닙니다.
4. [검증 보고서](VALIDATION_REPORT.md)의 Standalone·VSF·다수 장비 항목을 읽어 실장비 검증 여부를 따로 판단합니다.

## 면접에서 설명할 수 있는 범위와 남은 과제

- 설명할 설계: 읽기 전용 CLI 경계, SSH 지문 승인, 장비별 실패 격리, 동시 처리와 지연 재시도, 원자적 저장.
- 남은 실증: 허가된 2930F/VSF에서 수동 출력 대조, 펌웨어별 프롬프트와 SSH 정책, 실제 지연·취소·장시간 수집 기록.
- 보안 과제: [Paramiko 감사 예외](DEPENDENCY_AUDIT_EXCEPTIONS_KO.md)의 수정 버전 확인·회귀·배포 조건을 충족해야 예외를 제거할 수 있습니다. 문서에 명시된 v0.1.8 바이너리와 `main`의 SHA-1 차단 차이를 함께 확인해야 합니다.

실장비 검증과 업무 시간 절감률은 공개 테스트만으로 주장하지 않습니다. 성과를 추가할 때는 작업 수·장비 수·실패 조건·비교 방법을 기록한 비식별 결과가 필요합니다.
