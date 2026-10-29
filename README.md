# 장전 브리핑 — 국내 증시·경제 뉴스레터 에이전트

작성: Sung, Hyun-Joon · 아이펠 AI Agent 2기 · 에이전트 팀 꾸리기 3 [프로젝트]

경제 매체 5곳에서 최근 24시간 기사를 모아, 출근길 개인 투자자에게 중요한 5건을 고르고, 본문을 읽어 요약·인사이트를 쓴 뒤 원문 대조 검수를 통과한 것만 Discord로 발행하는 LangGraph 에이전트입니다.

![Discord에 도착한 브리핑](docs/discord.png)

## 독자

| 항목 | 내용 |
|---|---|
| 누구 | 국내 주식에 직접 투자하는 3~5년 차 직장인 |
| 언제 | 출근길 07:30~08:30, 휴대전화로 10분 |
| 판단 기준 | 오늘 국내 증시나 보유 종목 판단에 실제로 영향을 줄 만한가 |

독자·기준·숫자는 모두 [audience.yaml](audience.yaml)에 있습니다. 코드를 고치지 않고 이 파일만 바꿔 다른 독자용으로 쓸 수 있습니다.

## 동작 순서

| 단계 | 하는 일 |
|---|---|
| ① 수집 | 채택한 5곳(연합뉴스 마켓, 한국경제 증권, 매일경제 증권, 동아일보 경제, 뉴시스 경제)의 RSS에서 24시간 기사 수집 |
| ② 선별 | 제목 규칙으로 사전 필터 → 40건씩 예선(묶음당 4건) → 본선 순위 → 코드로 같은 주체 1건·매체당 2건·최대 5건 보장 |
| ③ 요약 | 기사마다 본문 추출(600자 미만은 제외) → 헤드라인·세 문장 요약·인사이트 한 문장. 한국어 비율과 인사이트 길이(120자)는 코드로 검사 |
| ④ 검수 | 다른 AI 호출이 요약을 원문과 대조 → 불합격이면 지적 사항을 넣어 1회 재생성 → 그래도 불합격이면 그 기사만 제외 |
| ⑤ 발행 | Discord 웹훅으로 전송, HTTP 200과 메시지 id로 도착 확인. 0건이면 "오늘은 조용합니다" 발행 |

소스 17곳을 어떻게 재서 5곳을 골랐는지는 [store/source_audit.md](store/source_audit.md), 전체 설계와 결과는 [REPORT.md](REPORT.md)에 있습니다.

## 실행 방법

### 1) 내 컴퓨터에서

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

`.env`에 `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL`을 채운 뒤 실행합니다.

| 목적 | 명령 |
|---|---|
| 키 없이 연결만 시험(가짜 AI, 발행 안 함) | `python run.py --mock` |
| 진짜 AI로 만들되 보내지 않음 | `python run.py --dry-run` |
| 실제 발행 | `.env`에 `DRY_RUN=0`을 적고 `python run.py` |
| 소스 다시 측정 | `python audit_sources.py` |
| 검수가 틀린 요약을 잡는지 시험 | `python check_verifier.py` |

`DRY_RUN`을 적지 않으면 기본값은 "보내지 않음"입니다.

### 2) GitHub Actions에서

저장소 **Settings → Secrets and variables → Actions**에 `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL`을 넣은 뒤, **Actions → 장전 브리핑 → Run workflow**로 실행합니다. "발행하지 않고 로그만 남기기"를 체크하면 보내지 않습니다.

평일 07:30 자동 예약은 현재 꺼 두었습니다. 다시 켜려면 [.github/workflows/daily.yml](.github/workflows/daily.yml)의 `schedule` 두 줄 앞 `#`을 지웁니다. 실행이 끝나면 기록(`store/metrics.jsonl`, `store/runs/`)이 저장소에 자동 커밋됩니다.

## 파일 구성

| 파일 | 내용 |
|---|---|
| `graph.py` | 파이프라인 전체(수집·선별·요약·검수·발행) |
| `run.py` | 실행 진입점(`--mock`, `--dry-run`, `--hours`) |
| `audience.yaml` | 독자, 판단 기준, 분류 라벨, 각종 숫자 |
| `sources.yaml` | 소스 후보 17곳과 채택 여부 |
| `audit_sources.py` | 소스 측정 → `store/source_audit.md` |
| `check_verifier.py` | 검수 시험 → `store/verifier_test.jsonl` |
| `REPORT.md` | 프로젝트 보고서 |
| `GUIDE.md` | 단계별 진행 가이드 |
| `PRESENTATION.md` | 발표 원고 |

## 알려진 한계

- 1차 소스(한국은행·금융위원회·정책브리핑)는 robots.txt 금지나 피드 오류로 쓰지 못했습니다.
- 검수는 금액·비율·방향이 틀린 요약은 잡았지만(5/5), 날짜 오기는 놓칠 수 있습니다.
- 인사이트를 "오늘 확인해 볼 포인트"로 끝내라는 규칙은 아직 코드로 검사하지 않습니다.

투자 권유가 아니며, 기사 원문 링크를 함께 발행합니다.
