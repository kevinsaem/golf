# ⛳ 티메이트 (TeeMate)

> **"다음 라운드, 누구랑 칠까? 내가 원하는 사람과 같은 조로."**

골프 라운딩 참가자들이 "같이 치고 싶은 사람"을 순위로 정하면,
전원의 만족도가 가장 높은 조편성을 자동으로 찾아주는 모바일 웹 서비스.

- 도메인: `golf.kevinsaem.com`
- 스택: FastAPI + Jinja2 + HTMX + Tailwind CSS (+ SQLite → PostgreSQL)

## 서비스 흐름

```
주최자: [구글 로그인] → 라운딩 만들기(조 개수·참석자 명단) → 공유 링크 받기 → 참가자에게 전달
참가자: 링크 → 본인 이름 선택 → 나머지 참가자를 희망 순서대로 줄 세우기
주최자: 전원 등록 확인 → 자동 배정 → 결과 검토·수정 → 확정
전원:   최종 조편성 확인
```

## 로그인 / 접속 정책

- **주최자**: 구글 로그인(OAuth). 로그인하면 자기가 만든 라운딩이 계정에 기록으로 남아,
  다음에 다시 와서 과거/진행 중인 라운딩을 모두 조회할 수 있다.
- **참가자**: 로그인 없음. 링크 + 명단에서 본인 이름 선택.
  첫 선택 시 브라우저에 라운딩별 식별 쿠키를 남겨, **링크 재접속 시**:
  - 모으는 중 → 전체 진행 현황("N명 중 M명 완료") + 내가 낸 희망 순위 보기/수정
  - 확정 후 → 최종 조편성 결과 표시

## 매칭 원리

1. 각 참가자가 나머지 전원을 **희망 순위**로 줄 세운다.
2. 순위를 점수로 바꾼다. (1지망 = 높은 점수)
3. 한 조 안에서 서로에게 준 점수의 합이 그 조의 만족도.
4. 모든 조편성을 따져 전체 만족도가 최대인 조합을 고른다.
   - 참가자가 적으면 **전수 계산(항상 최적)**, 많으면 **휴리스틱(거의 최적)**.
5. 서로가 서로를 원하는 **양방향 짝**에는 가산점.

## 개발 현황 (단계별)

- [x] **1단계 — 매칭 엔진** (`tools/matcher.py`, 테스트 17개 통과)
- [x] **2단계 — 서버 + 데이터** (주최자 구글로그인, 라운딩 생성·링크·저장)
- [x] **3단계 — 화면 8개** (모바일 우선, Jinja2 + Tailwind + SortableJS)
- [x] **4단계 — 테스트** (pytest 26개 통과 + 실서버 curl 검증) *(Playwright E2E는 추후)*
- [x] **5단계 — 배포** (golf.kevinsaem.com, cafe24 VPS, https 적용)

## 배포 (cafe24 VPS)

- 서버: cafe24 가상서버 `1.234.20.103` (SSH 별칭 `kevinsaem`), Ubuntu 22.04
- 앱 위치: `/opt/golf`, 파이썬 venv `/opt/golf/.venv`
- 서비스: systemd `golf.service` (uvicorn 127.0.0.1:8900, 자동재시작/부팅시작)
- 웹: nginx `golf.kevinsaem.com` → 127.0.0.1:8900, Let's Encrypt SSL(자동갱신)
- DB: SQLite `/opt/golf/teemate.db` (추후 PostgreSQL 이전 가능)
- 비밀: 서버 `/opt/golf/.env` (구글 OAuth 키, SECRET_KEY) — git 제외

```bash
# 코드 수정 후 재배포
ssh kevinsaem 'cd /opt/golf && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart golf'
```

## 개발 환경

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 테스트 실행
.venv/bin/python -m pytest -q
```
