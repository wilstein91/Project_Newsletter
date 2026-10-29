"""장전 브리핑 — 국내 증시·경제 뉴스레터 에이전트 (LangGraph)

수집(collect) → 선별(select) → 요약(report, 기사마다 워커) → 검수(verify) → 발행(publish)
독자·기준은 audience.yaml, 소스는 sources.yaml 에서 읽는다. 이 파일은 import 만 해서는 아무것도 실행하지 않는다.
"""
import json, operator, os, re, time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import feedparser, requests, trafilatura, yaml
from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
STORE = ROOT / "store"
load_dotenv(ROOT / ".env")

AUD = yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))
NUM = AUD["numbers"]
SOURCES = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
UA = {"User-Agent": "Mozilla/5.0 (newsletter-agent-course)"}
KST = timezone(timedelta(hours=9))


# ── State ──────────────────────────────────────────────────────────────
def merge(a: dict, b: dict) -> dict:          # 여러 노드가 stats 칸에 값을 더한다
    return {**a, **b}

class Brief(TypedDict):
    hours: int
    collected: list                            # ① 수집한 기사
    picked: list                               # ② 선별한 3~5건
    drafted: Annotated[list, operator.add]     # ③ 워커들이 나눠 채우는 초안
    verified: list                             # ④ 검수 통과(재생성 포함)
    trace: dict                                # 선별 근거(라벨·이유·경계·탈락 사유)
    stats: Annotated[dict, merge]              # metrics.jsonl 로 남길 숫자
    log: Annotated[list, operator.add]

class ReportIn(TypedDict):
    item: dict


# ── 공통 도구 ──────────────────────────────────────────────────────────
def fetch_feed(url: str):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content).entries

def entry_time(e):
    p = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*p[:6], tzinfo=timezone.utc) if p else None

def extract_body(url: str) -> str:
    try:
        html = requests.get(url, headers=UA, timeout=20).text
        return trafilatura.extract(html, include_comments=False) or ""
    except Exception:
        return ""

def korean_ratio(text: str) -> float:
    letters = re.findall(r"[가-힣A-Za-z]", text)
    return sum(1 for c in letters if "가" <= c <= "힣") / max(len(letters), 1)


# ── LLM ────────────────────────────────────────────────────────────────
_client = None
def llm_parse(system: str, user: str, schema):
    """구조화 출력 호출 한 번. LLM_MOCK=1 이면 키 없이 배선만 시험한다(발행 금지)."""
    if os.environ.get("LLM_MOCK") == "1":
        return _mock(schema, user)
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI()
    for attempt in range(3):                   # 일시적 오류(429·타임아웃)는 잠깐 쉬고 다시
        try:
            r = _client.chat.completions.parse(
                model=AUD["model"], temperature=0, response_format=schema,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            return r.choices[0].message.parsed
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


# ── ① 수집 ─────────────────────────────────────────────────────────────
def collect(s: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=s["hours"])
    items, seen, dead, per_source = [], set(), [], {}
    for src in [x for x in SOURCES if x.get("adopt")]:
        try:
            entries = fetch_feed(src["url"])
        except Exception:
            dead.append(src["name"])            # 한 곳이 죽어도 나머지는 계속 — 대신 기록한다
            continue
        n = 0
        for e in entries:
            at = entry_time(e)
            if not at or at < cutoff or not e.get("link"):
                continue
            key = e.link.split("?")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            items.append({"title": e.get("title", "").strip(), "url": e.link, "source": src["name"],
                          "tier": src.get("tier", 2), "at": at.isoformat(),
                          "summary": re.sub(r"<[^>]+>", "", e.get("summary", ""))[:300]})
            n += 1
        per_source[src["name"]] = n
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"collected": items,
            "stats": {"collected": len(items), "per_source": per_source, "dead_sources": dead},
            "log": [f"① 수집 {s['hours']}시간 · {len(items)}건 · 소스 {len(per_source)}곳"
                    + (f" · 응답 없음 {dead}" if dead else "")]}


# ── ② 선별: 2단계 상대평가 ──────────────────────────────────────────────
class Pick(BaseModel):
    index: int = Field(description="후보 목록에서의 번호")
    reason: str = Field(description="독자 기준에서 왜 골랐는지 한 문장")
    subject: str = Field(description="이 소식의 주인공인 회사·기관 이름 하나(예: 삼성전자, 연준, 한국은행, 산업통상부). 국가 표시(미·美) 없이")
    event: str = Field(description="이 기사가 다루는 사건을 짧은 라벨로. 같은 사건이면 같은 라벨")
    category: Literal[tuple(AUD["categories"])] = Field(description="분류 라벨 하나")   # 목록 밖 라벨은 스키마가 막는다

class Shortlist(BaseModel):
    picks: list[Pick] = Field(description="중요한 순서대로")

CRITERIA = (f"당신은 '{AUD['newsletter_name']}' 뉴스레터 편집자입니다.\n독자: {AUD['persona'].strip()}\n"
            f"판단 기준: {AUD['importance']}\n고를 것:\n" + "\n".join(f"- {x}" for x in AUD["pick"])
            + "\n버릴 것:\n" + "\n".join(f"- {x}" for x in AUD["exclude"])
            + "\n같은 사건을 다룬 기사는 하나만 고르세요.")

def ask_picks(cands: list, k: int) -> list[Pick]:
    """후보 중 중요한 순서대로 k건. 없는 번호·중복 번호는 코드로 버린다."""
    lines = "\n".join(f"[{i}] ({c['source']}) {c['title']} — {c['summary'][:120]}" for i, c in enumerate(cands))
    out = llm_parse(CRITERIA, f"후보 {len(cands)}건 중 가장 중요한 {k}건을 중요한 순서대로 고르세요.\n\n{lines}", Shortlist)
    seen, picks = set(), []
    for p in out.picks:
        if 0 <= p.index < len(cands) and p.index not in seen:
            seen.add(p.index); picks.append(p)
    return picks[:k]

def select(s: dict) -> dict:
    B, K, T = NUM["batch"], NUM["per_batch"], NUM["target"]
    pats = AUD.get("exclude_title_patterns", [])
    items = [x for x in s["collected"] if not any(p in x["title"] for p in pats)]   # 코드 사전 필터
    prefiltered = len(s["collected"]) - len(items)
    tier1 = [x for x in items if x["tier"] == 1][: NUM["tier1_cap"]]           # 1차 소스 우선 통과(상한)
    rest = [x for x in items if x not in tier1]
    trace = {"tier1_pass": [x["title"] for x in tier1], "batches": [], "final": [], "dropped_final": []}
    survivors = []
    for i in range(0, len(rest), B):                                          # 예선 — 묶음마다 K건 (+경계 1건)
        chunk = rest[i:i + B]
        ranked = ask_picks(chunk, K + 1)
        keep, edge = ranked[:K], ranked[K:]
        for p in keep:
            survivors.append({**chunk[p.index], "reason": p.reason, "event": p.event, "subject": p.subject, "category": p.category})
        trace["batches"].append({"size": len(chunk), "passed": len(keep),
                                 "last_in": chunk[keep[-1].index]["title"] if keep else None,
                                 "first_out": chunk[edge[0].index]["title"] if edge else None})
    pool = [{**x, "reason": "1차 소스 우선 통과", "event": x["title"][:20], "category": "정책·규제"} for x in tier1] + survivors
    if not pool:
        return {"picked": [], "trace": trace, "stats": {"prelim": 0, "picked": 0},
                "log": ["② 선별 후보 0건"]}
    ranked = ask_picks(pool, min(T + 3, len(pool)))                           # 본선 — 한 화면에서 순위
    subject = lambda p: re.sub(r"^(미국|美|미)\s*|\s", "", p.subject)            # 주체 이름을 같은 꼴로
    picked, events, per_src = [], Counter(), Counter()
    for p in ranked:                                                          # 프롬프트는 요청일 뿐 → 코드로 보장
        a = {**pool[p.index], "reason": p.reason, "event": p.event, "subject": p.subject, "category": p.category}
        why_out = ("같은 주체·사건 중복" if events[subject(p)] else
                   "매체 상한 초과" if per_src[a["source"]] >= NUM["per_source_cap"] else
                   "최종 건수 초과" if len(picked) >= T else None)
        if why_out:
            trace["dropped_final"].append({"title": a["title"], "source": a["source"], "subject": p.subject, "event": p.event, "why": why_out})
            continue
        events[subject(p)] += 1; per_src[a["source"]] += 1; picked.append(a)
    trace["final"] = [{k: a[k] for k in ("title", "source", "category", "subject", "event", "reason")} for a in picked]
    return {"picked": picked, "trace": trace,
            "stats": {"prefiltered": prefiltered, "prelim": len(survivors), "tier1_pass": len(tier1), "picked": len(picked),
                      "final_ratio": round(len(picked) / max(len(pool), 1), 2),
                      "categories": dict(Counter(a["category"] for a in picked)),
                      "picked_sources": dict(per_src)},
            "log": [f"② 선별 {len(s['collected'])} → 사전필터 -{prefiltered} → 예선 {len(survivors)}(+1차 {len(tier1)}) → {len(picked)}건 "
                    f"· 라벨 {dict(Counter(a['category'] for a in picked))}"]}


# ── ③ 요약: 기사마다 워커 ──────────────────────────────────────────────
class Draft(BaseModel):
    headline: str = Field(description="25자 내외의 한국어 헤드라인")
    summary: str = Field(description="본문에 근거한 세 문장 요약. ~합니다체, 과장 없이 건조하게, 한국어")
    why: str = Field(description=AUD["insight_rule"].strip() + f" {NUM['why_max']}자 이내.")

SYS_DRAFT = (f"당신은 '{AUD['newsletter_name']}' 기자입니다. 독자: {AUD['persona'].strip()}\n"
             "아래 기사 본문만 근거로 헤드라인·요약·인사이트(why)를 한국어로 쓰세요. 본문에 없는 숫자·사실을 더하지 마세요.\n"
             "'주목된다·기대를 모은다' 같은 기자체 표현은 쓰지 마세요.\n"
             f"why 칸은 반드시 한 문장, {NUM['why_max']}자 이내입니다. 본문에 없는 전망·해석을 지어내지 마세요.")

def draft(body: str, feedback: str = "") -> Draft:
    user = f"[기사 본문]\n{body[:6000]}"
    if feedback:
        user += f"\n\n[이전 요약이 검수에서 지적받은 점 — 반드시 고칠 것]\n{feedback}"
    d = llm_parse(SYS_DRAFT, user, Draft)
    fix = []                                                                  # 프롬프트는 요청일 뿐 → 코드로 검사
    if korean_ratio(d.headline + d.summary) < NUM["min_korean"]:
        fix.append("모든 칸을 한국어로만 쓰세요.")
    if len(d.why) > NUM["why_max"]:
        fix.append(f"why 칸은 한 문장, {NUM['why_max']}자 이내로 줄이세요. 본문에 없는 해석·전망은 빼세요.")
    if fix:
        d = llm_parse(SYS_DRAFT, user + "\n\n[다시 쓸 때 지킬 것]\n" + "\n".join(fix), Draft)
    if len(d.why) > NUM["why_max"]:                                           # 그래도 길면 첫 문장만 남긴다
        d.why = re.split(r"(?<=[.다])\s", d.why.strip(), maxsplit=1)[0]
    return d

def fan_report(s: dict):
    if not s["picked"]:                                   # 0건이어도 검수·발행까지 가서 "조용합니다"를 보낸다
        return "verify"
    return [Send("report", {"item": it}) for it in s["picked"]]

def report(s: ReportIn) -> dict:
    it = s["item"]
    body = extract_body(it["url"])
    if len(body) < NUM["min_body"]:                                           # G1 기준선
        return {"drafted": [{**it, "skipped": f"본문 {len(body)}자"}],
                "log": [f"  ③ 취재 제외 {it['source']} · 본문 {len(body)}자"]}
    try:
        d = draft(body)
    except Exception as ex:
        return {"drafted": [{**it, "skipped": f"요약 실패 {type(ex).__name__}"}],
                "log": [f"  ③ 요약 실패 {it['source']} · {type(ex).__name__}"]}
    return {"drafted": [{**it, "body": body[:6000], "body_len": len(body), **d.model_dump()}]}


# ── ④ 검수: LLM 원문 대조 + 재생성 1회 → 그래도 탈락이면 스킵 ─────────
class Verdict(BaseModel):
    ok: bool = Field(description="헤드라인과 요약의 모든 사실·숫자가 원문에서 뒷받침되면 true")
    problems: list[str] = Field(description="원문에 없거나 원문과 다른 부분. 없으면 빈 목록")

SYS_CHECK = ("당신은 경제 뉴스 팩트체커입니다. 요약이 원문에서 뒷받침되는지 판정하세요.\n"
             "헤드라인과 요약만 판단합니다(인사이트 칸은 보지 않음). 번역·단위 환산·반올림은 문제가 아닙니다.\n"
             "숫자·기관명·방향(상승/하락, 인상/인하)이 원문과 다르거나 원문에 없으면 불합격입니다.")

def check(d: dict) -> Verdict:
    return llm_parse(SYS_CHECK, f"[원문]\n{d['body'][:5000]}\n\n[헤드라인] {d['headline']}\n[요약] {d['summary']}", Verdict)

def verify(s: dict) -> dict:
    kept, skipped, regen_ok, fails, verdicts = [], [], 0, 0, []
    for d in s["drafted"]:
        if d.get("skipped"):
            skipped.append({"title": d["title"], "why": d["skipped"]}); continue
        v, tries = check(d), 0
        first = {"summary": d["summary"], "problems": v.problems}              # 1차 판정을 남겨 두어 무엇이 걸렸는지 보인다
        if not v.ok:
            fails += 1
        while not v.ok and tries < NUM["max_regen"]:                          # 대안 1: 지적 사항을 주고 재생성
            tries += 1
            d = {**d, **draft(d["body"], "\n".join(v.problems)).model_dump()}
            v = check(d)
            regen_ok += v.ok
        verdicts.append({"title": d["title"], "ok": v.ok, "regen": tries, "problems": v.problems,
                         **({"first_try": first} if tries else {})})
        if v.ok:
            kept.append({**d, "regen": tries})
        else:                                                                 # 대안 2: 스킵하고 기록
            skipped.append({"title": d["title"], "why": "검수 탈락: " + "; ".join(v.problems)[:200]})
    return {"verified": kept, "trace": {**s["trace"], "verdicts": verdicts, "skipped": skipped},
            "stats": {"drafted": len([d for d in s["drafted"] if not d.get("skipped")]),
                      "body_ok_rate": round(1 - sum("본문" in x["why"] for x in skipped) / max(len(s["drafted"]), 1), 2),
                      "verify_fail": fails, "regen_pass": regen_ok, "verified": len(kept), "skipped": len(skipped)},
            "log": [f"④ 검수 {len(s['drafted'])} → {len(kept)}건 · 1차 불합격 {fails} · 재생성 후 합격 {regen_ok} · 스킵 {len(skipped)}"]}


# ── ⑤ 발행: Discord embed, 기본은 dry-run ─────────────────────────────
COLOR = 0x1F6F5C

def build_embeds(arts: list, today: str) -> list:
    head = {"title": f"📈 {AUD['newsletter_name']} · {today}", "color": COLOR,
            "description": (f"장 시작 전 확인할 소식 {len(arts)}건입니다. (" + ", ".join(sorted({a['source'] for a in arts})) + ")")
            if arts else "오늘은 조용합니다 — 기준을 통과한 소식이 없습니다. (파이프라인은 정상 실행됨)"}
    cards = [{"title": f"{i}. {a['headline']}"[:256], "url": a["url"], "color": COLOR,
              "description": f"{a['summary']}\n\n💡 **{a['why']}**"[:4000],
              "footer": {"text": f"{a['source']} · {a['category']} · {a['when']}"}} for i, a in enumerate(arts, 1)]
    embeds = [head] + cards
    while len(embeds) > 1 and sum(len(json.dumps(e, ensure_ascii=False)) for e in embeds) > 5800:
        embeds.pop()                                                          # 한도 6,000자 — 뒤에서부터 덜어 낸다
    return embeds[:10]

def publish(s: dict) -> dict:
    arts = [{"headline": a["headline"], "summary": a["summary"], "why": a["why"], "url": a["url"],
             "source": a["source"], "category": a.get("category", ""),
             "when": datetime.fromisoformat(a["at"]).astimezone(KST).strftime("%m-%d %H:%M")} for a in s["verified"]]
    today = datetime.now(KST).strftime("%Y-%m-%d")
    payload = {"username": AUD["newsletter_name"], "embeds": build_embeds(arts, today)}
    STORE.mkdir(exist_ok=True)
    (STORE / "last_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    dry = os.environ.get("DRY_RUN", "1") == "1" or os.environ.get("LLM_MOCK") == "1" or not hook
    if dry:
        return {"stats": {"published": 0, "dry_run": True},
                "log": [f"⑤ 발행 [dry-run] embed {len(payload['embeds'])}개 — 보내지 않음 (store/last_payload.json)"]}
    r = requests.post(hook + "?wait=true", json=payload, timeout=20)          # wait=true → 200 + 메시지 id 로 도착 확인
    ok = r.status_code == 200 and "id" in r.json()
    return {"stats": {"published": len(arts) if ok else 0, "dry_run": False, "http": r.status_code,
                      "message_id": r.json().get("id") if ok else None},
            "log": [f"⑤ 발행 {'성공' if ok else '실패'} HTTP {r.status_code} · 기사 {len(arts)}건"]}


# ── 그래프 ─────────────────────────────────────────────────────────────
def build():
    g = StateGraph(Brief)
    for name in ("collect", "select", "report", "verify", "publish"):
        g.add_node(name, globals()[name])
    g.add_edge(START, "collect")
    g.add_edge("collect", "select")
    g.add_conditional_edges("select", fan_report, ["report", "verify"])
    g.add_edge("report", "verify")
    g.add_edge("verify", "publish")
    g.add_edge("publish", END)
    return g.compile()

def run(hours: int | None = None) -> dict:
    t0 = time.time()
    out = build().invoke({"hours": hours or NUM["hours"], "collected": [], "picked": [], "drafted": [],
                          "verified": [], "trace": {}, "stats": {}, "log": []})
    now = datetime.now(KST)
    rec = {"run_at": now.isoformat(timespec="seconds"), "mock": os.environ.get("LLM_MOCK") == "1",
           "seconds": round(time.time() - t0, 1), **out["stats"]}
    STORE.mkdir(exist_ok=True)
    with open(STORE / "metrics.jsonl", "a", encoding="utf-8") as f:           # 실행마다 한 줄씩 쌓는다
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (STORE / "runs").mkdir(exist_ok=True)
    detail = {"run": rec, "trace": out["trace"], "log": out["log"],
              "verified": [{k: a.get(k) for k in ("title", "url", "source", "category", "headline", "summary", "why", "regen")}
                           for a in out["verified"]]}
    (STORE / "runs" / f"{now:%Y-%m-%d_%H%M}.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ── 키 없이 배선만 시험하는 가짜 LLM (LLM_MOCK=1) ───────────────────────
def _mock(schema, user):
    if schema is Shortlist:
        k = int(re.search(r"가장 중요한 (\d+)건", user).group(1))
        n = len(re.findall(r"^\[\d+\]", user, re.M))
        return Shortlist(picks=[Pick(index=i, reason="(mock)", subject=f"s{i}", event=f"e{i}", category="기타") for i in range(min(k, n))])
    if schema is Draft:
        body = user.split("]\n", 1)[1]
        return Draft(headline="(모의) 헤드라인", summary="(모의) " + body[:120].replace("\n", " "), why="(모의) 확인 포인트")
    if schema is Verdict:
        return Verdict(ok=True, problems=[])
