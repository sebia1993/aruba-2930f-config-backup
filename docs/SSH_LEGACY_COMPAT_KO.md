# Aruba 2930F WC.16.01.0008 SSH 협상 실패 대응 (v0.9.4)

## 현장 오류
`SSH_ALGORITHM_INCOMPATIBLE` / `The SSH server and client do not share a compatible host-key or key-exchange algorithm.` 발생 → 포트·LLDP 상태 미수집 → 일괄 PoE ON 안전 보류. LLDP 실패는 별도 장애가 아니라 최초 SSH 실패의 연쇄 결과일 수 있습니다.

## 제조사 근거
HPE 공식 ArubaOS-Switch WC 16.08 릴리스 노트의 16.08.0021 항목은 새 서버 호스트키 알고리즘 `rsa-sha2-256`, `rsa-sha2-512` 및 `ecdh-sha2-nistp256/384/521`, `diffie-hellman-group-exchange-sha256` 등 KEX 추가를 명시합니다. 사용 장비 WC.16.01.0008은 이보다 이전 버전이지만, 현장 실제 활성 알고리즘은 **`show ip ssh` 출력 확인이 필요합니다.**

HPE 출처: https://arubanetworking.hpe.com/techdocs/AOS-Switch-RN/Content/16.08RN/WC/Enhancements.htm

## 사용 절차
1. v0.9.4 Windows ZIP을 실행하고 상단 SSH 공통 계정을 입력합니다.
2. 포트 통합 관리에서 스위치와 포트를 입력한 뒤 **전체 조회**합니다.
3. `SSH_ALGORITHM_INCOMPATIBLE`로 실패하면 검색/필터 영역의 **레거시 SSH (장비별)** 버튼을 선택합니다.
4. 정확한 대상 **IP:SSH 포트**를 지정하고 화면에 표시된 값을 확인란에 직접 입력합니다. 프로그램 전체·서브넷·다른 포트는 승인되지 않습니다.
5. 예외를 승인하면 해당 스위치만 **읽기 전용 재조회**합니다. 처음 보는 SSH 서버 공개키 지문이라면 화면에 나오는 **SHA-256 지문을 별도의 신뢰할 수 있는 경로**로 확인한 뒤 승인해야 합니다.
6. 포트 상태가 정상 수집된 것을 확인한 다음 기존 `범위 전체 선택 → PoE ON 즉시`를 실행합니다. 여전히 미확인 포트가 있으면 전체 변경을 차단합니다.
7. 예외를 해제하려면 같은 버튼에서 같은 IP:포트를 확인한 뒤 **선택 장비의 예외 해제**를 실행합니다. 계정 변경·세션 해제·프로그램 종료 시 모든 예외가 소멸합니다.

## 보안 범위와 제한
- 예외는 **서버 호스트키 서명 `ssh-rsa` (RSA/SHA-1)**에만 적용합니다. `ssh-rsa` 사용자 공개키 인증은 계속 차단합니다.
- 동의 없는 자동 재시도/보안 강등, Telnet 대체 연결, 호스트키 자동 승인, 장비 설정 자동 변경은 없습니다.
- RSA/SHA-1 연결은 향상된 알고리즘보다 위험합니다. SHA-256 **지문 고정은 서버 키 변경 탐지**이며 SHA-1 서명 위험 자체를 없애지 못합니다.
- `diffie-hellman-group1-sha1`처럼 더 약한 키 교환은 새로 허용하지 않습니다. 실제 장비가 호스트키 이외에 지원하지 않는 KEX/cipher만 제공하면 접속은 여전히 실패할 수 있습니다.
- 구형 펌웨어 운영망 확장 전 HPE의 공식 펌웨어 업그레이드 가능성 검토를 권장합니다.
- **가상 루프백 SSH 테스트 성공은 WC.16.01.0008 실장비 테스트 성공을 보증하지 않습니다.**

## 현장 진단
SecureCRT/콘솔에서 `show version`, `show ip ssh` (가능한 경우)를 확인합니다. 실제 IP와 계정 암호를 외부 보고서나 이슈에 게시하지 말고, 펌웨어 버전과 Host Key, KEX, Cipher 항목만 비식별 처리해 기록합니다.
