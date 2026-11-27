# banron-desk-runner

반론보도닷컴 발제 데스크의 자동 수집 실행 자리.

## 네이버 뉴스 검색 · 데이터랩 수집

- `naver/config.json` — 뉴스 검색어(분류별)와 검색어 트렌드 비교 묶음(묶음당 주제어 최대 5개)
- `naver/collect.py` — 수집기. 인증키는 저장소 비밀값 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` 에서만 읽는다
- `.github/workflows/naver-collect.yml` — 평일 07:30, 15:30(한국시간) 자동 실행, 수동 실행 가능
- `data/naver/news.json`, `data/naver/trends.json` — 최신 수집 결과. 발제 데스크 예약 작업이 읽어 앱에 반영한다

인증키를 코드나 설정 파일에 적지 않는다.

## 오타 체크 원문 수집

- `typo/fetch.py` — 반론보도닷컴에서 시간 범위 안 기사의 제목·부제·본문·사진 설명을 받는다. 모든 요청은 하나씩, 2초 이상 간격(차단 방지)
- `.github/workflows/typo-fetch.yml` — 수동 실행(workflow_dispatch: id, start, end, extra). '반론보도 오타 체크' 예약 작업이 실행을 요청하고 결과를 읽는다
- `data/typo/<id>.json` — 수집 결과. 3일 지난 파일은 다음 수집 때 지운다
