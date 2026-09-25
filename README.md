# Parallel Backup

현재 버전: **1.6.2**

Windows에서 사용하는 GUI 기반 병렬 백업 프로그램입니다.

## 핵심 기능

### 병렬 백업
- 하나의 원본 폴더를 여러 경로에 동시에 백업
- 대상별 병렬도 제한
- 대상 수가 많아도 설정한 worker 수만큼만 동시에 실행

### 이름/ZIP 스냅샷
- 백업 이름 + 날짜/시간 자동 생성
- 같은 초에 여러 번 실행해도 `_01`, `_02` 식으로 충돌 회피
- ZIP은 첫 번째 백업 대상에서 직접 생성하며 시스템 TEMP에 전체 staging을 만들지 않음
- 생성 중 ZIP은 `.parallel-backup-build-*.zip.partial`로 작성
- 대상 복사 중에는 `.parallel-backup.partial-*.zip`을 사용
- 검증 성공 후에만 최종 ZIP 이름으로 원자적으로 확정

### 증분 백업
- 각 대상 경로의 최근 **검증 완료 ZIP**을 기준으로 비교
- 변경되지 않은 파일은 이전 검증 ZIP에서 직접 재사용
- 이전 버전(v2) manifest도 증분 기준으로 호환

### SHA-256 무결성
- 정밀 검사 백업에서 원본/ZIP/복구 대상의 파일 내용을 SHA-256으로 검증
- 누락/변조/복사 오류를 탐지
- 검증 완료 ZIP만 이후 증분 백업의 기준으로 사용
- 복구 후에도 SHA-256 재검증


### ZIP 아카이브 자동 생성
- 검증된 ZIP을 각 백업 대상에 저장
- ZIP 내부에도 `.parallel-backup/manifest.json`을 포함
- 생성된 ZIP의 CRC와 파일별 SHA-256을 다시 검증한 뒤 최종 ZIP으로 확정
- 기존 검증 ZIP은 다음 증분 백업의 기준으로 유지
- 오래된 검증 ZIP은 보존 정책에 따라 삭제

### 정밀 검사 원칙
- 정밀 검사 모드에서는 파일별 SHA-256을 매번 실제 계산
- 이전 메타데이터만으로 SHA-256을 재사용하지 않음
- 취소 요청은 원본 스캔/해시 계산/ZIP 생성/검증에 전달됨


### 제외 패턴
쉼표 또는 줄바꿈으로 패턴을 지정할 수 있습니다.

예:

```
.git
Library
Temp
*.log
*.tmp
node_modules
```

파일명뿐 아니라 상대 경로 기준 패턴도 처리합니다.

### 디스크 사전 검사
하드링크로 재사용할 수 있는 파일은 실제 추가 저장공간 계산에서 제외하고, 새로 써야 하는 파일의 크기를 기준으로 여유공간을 검사합니다.

### 보존 정책
대상별로 최신 N개의 **검증 완료** 스냅샷을 유지하고 오래된 스냅샷은 자동 삭제합니다.

### 장애 대응
- 24시간 이상 남아 있는 중간 `.partial` 폴더 자동 정리
- 검증 실패/취소된 ZIP은 증분 기준으로 사용하지 않음
- 작업 중 취소 가능

### 설정 프로필
설정은 자동 저장됩니다.

```
%LOCALAPPDATA%\ParallelBackup\profile.json
```

저장 항목:
- 원본
- 백업 이름
- 백업 대상
- 증분/검증 옵션
- 병렬도
- 보존 개수
- 제외 패턴

## GUI

- **병렬 백업 시작**
- **취소**
- **복구**
- **프로필 저장**
- 원본/대상 경로 선택
- 진행률
- 상세 로그

## 실행

Python 3.10 이상 권장.

```powershell
python app.py
```

또는 `ParallelBackup.bat`를 실행할 수 있습니다. 두 실행 경로는 동일한 백업 엔진을 사용합니다.

외부 Python 패키지는 필요하지 않습니다.

## 예시

원본:

```
C:\ai\uni_mcp
```

이름:

```
uni_mcp
```

대상:

```
D:\Backups
E:\Backups
F:\Backups
```

결과:

```
D:\Backups\uni_mcp_20260921_193000\
D:\Backups\uni_mcp_20260921_193000.zip
E:\Backups\uni_mcp_20260921_193000\
E:\Backups\uni_mcp_20260921_193000.zip
F:\Backups\uni_mcp_20260921_193000\
F:\Backups\uni_mcp_20260921_193000.zip
```

## 권장 설정

대형 개발 프로젝트에는 보통 다음 구성이 적합합니다.

```
증분 백업       ON
SHA-256 검증    ON
동시 대상 수    2~4
보존 스냅샷     5~10
```


## 주의

- 백업 대상은 원본 폴더 내부에 두지 마세요.
- 복구 시 ZIP/manifest 경로는 대상 폴더 밖으로 탈출할 수 없도록 검증합니다.
- 중요한 데이터는 서로 다른 물리 드라이브 또는 다른 저장 위치에 복수 백업하는 것을 권장합니다.


## UI 디자인 v1.6.2

GUI는 제공된 **Corporate Trust** 디자인 시스템을 기준으로 전면 재구성했습니다.

- Slate 50 계열의 밝은 배경
- Indigo 600 + Violet 600 포인트 컬러
- Indigo → Violet 그라디언트 헤더
- 흰색 elevated card 구조
- 카드형 SOURCE / BACKUP POLICY / EXCLUSIONS / DESTINATIONS 영역
- 대상 수 / 보존 ZIP 수 / 병렬 worker 수를 상단 metric card로 표시
- 상태 영역과 진행률을 분리
- 활동 로그를 별도 카드로 구성
- Plus Jakarta Sans가 설치된 환경에서는 해당 폰트를 사용하고, 없으면 Segoe UI로 자동 폴백
- 입력/버튼/진행률/상태의 색상과 포커스 스타일 통일


## 1.6.2 Hardening

- 시스템 TEMP에 원본 전체를 staging하지 않습니다.
- 첫 번째 백업 대상에서 ZIP을 직접 구성합니다.
- ZIP 누락/추가 멤버, 중복 이름, CRC 오류를 검사합니다.
- 정밀 모드에서는 파일별 SHA-256과 대상 ZIP SHA-256을 검증합니다.
- 복구 시 ZIP 및 manifest 경로 탈출을 차단합니다.
- 작업 취소는 원본 스캔, 해시 계산, ZIP 생성, ZIP 검증 및 복사에 전달됩니다.
- 자동 업데이트는 app.py, ParallelBackup.bat, parallel_backup_launcher.py를 함께 교체합니다.
