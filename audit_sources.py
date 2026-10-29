"""소스 후보 전체를 같은 기준으로 재서 소스 채택표를 만든다(매일 돌리지 않는다 — 채택할 때·의심될 때만).

  python audit_sources.py        →  store/source_audit.json, store/source_audit.md

관문
  G1 본문   : 최근 기사 3건의 원문에서 뽑은 본문이 min_body(600자) 이상인 비율 ≥ 2/3
  G2 생존   : 최근 24시간 안 글 ≥ 3건
  G3 수집허용: 피드 응답 200, robots.txt 가 피드·기사 주소 수집을 막지 않음
  + 중복    : 이미 채택한 소스와 기사 제목이 50% 넘게 겹치면 탈락(같은 기사 재전송)
등급 평가(관문 통과 소스끼리)
  관련도    : 24시간 글 제목 중 증시·거시 키워드가 들어간 비율
  매체당 1개 피드만, 관련도 높은 순으로 ADOPT_N 곳 채택 · 나머지는 '예비'
"""
import re
import json, sys, time
from datetime import datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urlparse

import requests

KEYWORDS = re.compile(r"코스피|코스닥|증시|주가|주식|금리|환율|달러|외국인|기관|채권|국채|실적|상장|공모|한은|연준|Fed|유가|물가|수출|반도체|ETF|시총|배당|공시")
ADOPT_N = 5

from graph import NUM, SOURCES, STORE, UA, entry_time, extract_body, fetch_feed

def robots_ok(url: str) -> bool | None:
    p = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=UA, timeout=10)
        if r.status_code >= 400:
            return True                       # robots.txt 가 없으면 제한 없음
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA["User-Agent"], url)
    except Exception:
        return None

def audit(src: dict) -> dict:
    row = {"name": src["name"], "tier": src["tier"], "url": src["url"]}
    t = time.time()
    try:
        entries = fetch_feed(src["url"])
        row["http"] = 200
    except requests.HTTPError as ex:
        row.update(http=ex.response.status_code, entries=0, verdict="탈락", why=f"G3 피드 응답 {ex.response.status_code}")
        return row
    except Exception as ex:
        row.update(http=None, entries=0, verdict="탈락", why=f"G3 접속 실패 {type(ex).__name__}")
        return row
    row["seconds"] = round(time.time() - t, 1)
    now = datetime.now(timezone.utc)
    ages = [(now - a).total_seconds() / 3600 for a in map(entry_time, entries) if a]
    row["entries"] = len(entries)
    row["last24h"] = sum(1 for a in ages if a <= 24)
    row["newest_h"] = round(min(ages), 1) if ages else None
    row["rss_summary_len"] = round(sum(len(e.get("summary", "")) for e in entries[:5]) / max(min(len(entries), 5), 1))
    sample = [e for e in entries if e.get("link")][:3]
    lens = [len(extract_body(e.link)) for e in sample]
    row["body_lens"] = lens
    row["g1_rate"] = round(sum(l >= NUM["min_body"] for l in lens) / max(len(lens), 1), 2)
    row["robots_ok"] = robots_ok(src["url"]) is not False and all(robots_ok(e.link) is not False for e in sample[:1])
    row["titles"] = [e.get("title", "").strip() for e in entries if (a := entry_time(e)) and (now - a) <= timedelta(hours=24)]
    row["relevance"] = round(sum(bool(KEYWORDS.search(x)) for x in row["titles"]) / max(len(row["titles"]), 1), 2)
    return row

def decide(rows: list) -> None:
    adopted_titles = set()
    for r in sorted(rows, key=lambda r: (r["tier"], -(r.get("last24h") or 0))):
        if "verdict" in r:
            continue
        overlap = (sum(t in adopted_titles for t in r["titles"]) / len(r["titles"])) if r["titles"] else 0
        r["overlap"] = round(overlap, 2)
        fails = []
        if r["g1_rate"] < 2 / 3: fails.append(f"G1 본문 {r['g1_rate']:.0%}")
        if r["last24h"] < (1 if r["tier"] == 1 else 3): fails.append(f"G2 24시간 {r['last24h']}건")
        if not r["robots_ok"]: fails.append("G3 robots.txt 금지")
        if overlap > 0.5: fails.append(f"중복 {overlap:.0%}")
        r["verdict"], r["why"] = ("탈락", " · ".join(fails)) if fails else ("통과", "관문 통과")
        if not fails:
            adopted_titles |= set(r["titles"])
    # 등급 평가: 매체당 피드 1개, 관련도 순 ADOPT_N 곳
    passed = sorted([r for r in rows if r["verdict"] == "통과"], key=lambda r: -r["relevance"])
    outlets, n = set(), 0
    for r in passed:
        outlet = r["name"].split()[0]
        if outlet in outlets:
            r["verdict"], r["why"] = "예비", f"같은 매체 다른 피드가 관련도 더 높음 (관련도 {r['relevance']:.0%})"
        elif n >= ADOPT_N:
            r["verdict"], r["why"] = "예비", f"관련도 {r['relevance']:.0%} — 상위 {ADOPT_N}곳 밖"
        else:
            r["verdict"], r["why"] = "채택", f"관문 통과 · 관련도 {r['relevance']:.0%} (상위 {ADOPT_N})"
            outlets.add(outlet); n += 1

def to_md(rows: list) -> str:
    head = ("| 소스 | 구분 | 응답 | 피드 건수 | 24h 글 | 최신 글(시간 전) | RSS 요약 길이 | 본문 추출(자) | G1 | robots | 중복 | 관련도 | 판정 | 근거 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    body = [f"| {r['name']} | {'1차' if r['tier'] == 1 else '2차'} | {r.get('http')} | {r.get('entries', 0)} | {r.get('last24h', '-')} | "
            f"{r.get('newest_h', '-')} | {r.get('rss_summary_len', '-')} | {'/'.join(map(str, r.get('body_lens', []))) or '-'} | "
            f"{r['g1_rate']:.0%} | {'허용' if r.get('robots_ok') else '금지'} | {r.get('overlap', 0):.0%} | {r.get('relevance', 0):.0%} | **{r['verdict']}** | {r['why']} |"
            if "g1_rate" in r else
            f"| {r['name']} | {'1차' if r['tier'] == 1 else '2차'} | {r.get('http')} | 0 | - | - | - | - | - | - | - | - | **{r['verdict']}** | {r['why']} |"
            for r in rows]
    return head + "\n" + "\n".join(body)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for s in SOURCES:
        print("측정 중:", s["name"], flush=True)
        rows.append(audit(s))
    decide(rows)
    STORE.mkdir(exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")
    (STORE / "source_audit.json").write_text(json.dumps({"measured_at": stamp, "rows": [{k: v for k, v in r.items() if k != "titles"} for r in rows]},
                                                        ensure_ascii=False, indent=2), encoding="utf-8")
    md = f"# 소스 채택표\n\n측정: {stamp} · 기준 G1 본문 ≥{NUM['min_body']}자 비율 ≥ 2/3, G2 24시간 글 ≥3(1차 소스 ≥1), G3 응답 200·robots 허용, 채택 소스와 제목 중복 ≤50%\n\n" + to_md(rows) + "\n"
    (STORE / "source_audit.md").write_text(md, encoding="utf-8")
    print("\n" + md)
