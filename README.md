# Parallel Backup

Windows에서 사용하는 간단한 GUI 병렬 백업 프로그램입니다.

## 기능

- 원본 폴더 선택
- 원하는 백업 이름 지정
- `이름_YYYYMMDD_HHMMSS` 형식으로 백업 폴더 생성
- 여러 백업 대상 경로 등록
- 여러 대상에 동시에 병렬 복사
- 진행 로그
- 원본 내부 경로를 백업 대상으로 지정하는 실수 방지
- 기존 동일 이름 폴더가 있으면 해당 대상만 실패 처리

## 실행

Python 3.10+ 권장.

```powershell
python app.py
```

외부 패키지가 필요하지 않습니다.

## 예시

원본:

`C:\ai\uni_mcp`

이름:

`uni_mcp`

백업 대상:

- `D:\Backups`
- `E:\Backups`
- `F:\Backups`

실행 결과:

```
D:\Backups\uni_mcp_20260921_193000
E:\Backups\uni_mcp_20260921_193000
F:\Backups\uni_mcp_20260921_193000
```

세 경로에 동시에 복사합니다.

## 이후 확장 후보

- 증분 백업
- 파일 해시 검증
- 백업 후 무결성 검증
- 제외 패턴
- 압축 백업
- 백업 취소
- 백업 기록
- Windows `.exe` 빌드
