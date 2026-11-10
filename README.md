# banron-desk-runner

반론보도닷컴 발제 데스크의 자동 수집 실행 자리.

## 네이버 뉴스 검색 · 데이터랩 수집

- `naver/config.json` — 뉴스 검색어(분류별)와 검색어 트렌드 비교 묶음(묶음당 주제어 최대 5개)
- `naver/collect.py` — 수집기. 인증키는 저장소 비밀값 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` 에서만 읽는다
- `.github/workflows/naver-collect.yml` — 평일 07:30, 15:30(한국시간) 자동 실행, 수동 실행 가능
- `data/naver/news.json`, `data/naver/trends.json` — 최신 수집 결과. 발제 데스크 예약 작업이 읽어 앱에 반영한다

인증키를 코드나 설정 파일에 적지 않는다.
