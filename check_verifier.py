"""검수 노드가 진짜 오류를 잡는지 시험한다 (조용한 실패 확인용 · API 키 필요).

  python check_verifier.py

가장 최근 실행(store/runs/)에서 발행된 기사 하나를 골라
  ① 원래 요약 → 합격해야 정상
  ② 숫자를 10배로 바꾼 요약 → 불합격해야 정상
  ③ 방향(상승↔하락, 인상↔인하)을 뒤집은 요약 → 불합격해야 정상
결과를 store/verifier_test.jsonl 에 한 줄씩 남긴다.
"""
import json, re, sys
from datetime import datetime

from graph import KST, STORE, check, extract_body

FLIP = {"상승": "하락", "하락": "상승", "인상": "인하", "인하": "인상", "증가": "감소", "감소": "증가", "순매수": "순매도", "순매도": "순매수"}

def tamper_number(text: str) -> str | None:
    # 날짜(2026년·28일·11월)가 아니라 금액·비율 숫자를 바꾼다 — 날짜를 바꾸면 '오타'로 넘어가 시험이 약해진다
    m = re.search(r"\d[\d,\.]*(?=\s*(조|억|만|천|%|원|달러|배|bp|포인트))", text)
    if not m:
        return None
    n = m.group().replace(",", "")
    try:
        bigger = str(int(float(n) * 10)) if "." not in n else str(round(float(n) * 10, 2))
    except ValueError:
        return None
    return text[:m.start()] + bigger + text[m.end():]

def tamper_direction(text: str) -> str | None:
    for a, b in FLIP.items():
        if a in text:
            return text.replace(a, b, 1)
    return None

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    runs = sorted((STORE / "runs").glob("*.json"))
    arts = [a for r in reversed(runs) for a in json.loads(r.read_text(encoding="utf-8")).get("verified", []) if a.get("summary")]
    if not arts:
        sys.exit("발행된 기사가 있는 실행 기록이 없습니다. 먼저 python run.py --dry-run 을 실행하세요.")
    results = []
    for a in arts[:3]:
        body = extract_body(a["url"])
        cases = [("원래 요약", a["summary"], True),
                 ("숫자 10배", tamper_number(a["summary"]), False),
                 ("방향 뒤집기", tamper_direction(a["summary"]), False)]
        for name, summ, expect in cases:
            if summ is None:
                continue
            v = check({"body": body, "headline": a["headline"], "summary": summ})
            good = v.ok == expect
            results.append({"title": a["title"], "case": name, "expect_ok": expect, "got_ok": v.ok, "pass": good, "problems": v.problems})
            print(f"{'✅' if good else '❌'} {name:6} 기대={'합격' if expect else '불합격'} 결과={'합격' if v.ok else '불합격'} · {a['title'][:40]}")
    with open(STORE / "verifier_test.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(KST).isoformat(timespec="seconds"), "results": results}, ensure_ascii=False) + "\n")
    caught = [r for r in results if not r["expect_ok"]]
    print(f"\n일부러 틀린 요약 적발 {sum(r['pass'] for r in caught)}/{len(caught)} · 정상 요약 통과 "
          f"{sum(r['pass'] for r in results if r['expect_ok'])}/{sum(r['expect_ok'] for r in results)}")

    # ④ 예외 처리 경로: 틀린 요약을 검수 노드에 그대로 넣으면 → 재생성 후 합격 또는 스킵, 그리고 멈추지 않아야 한다
    from graph import verify
    a = arts[0]
    bad = {**a, "at": "", "body": extract_body(a["url"]), "summary": tamper_number(a["summary"]) or a["summary"]}
    out = verify({"drafted": [bad], "trace": {}})
    st, v = out["stats"], out["trace"]["verdicts"][0]
    print(f"\n[예외 처리 경로] 1차 불합격 {st['verify_fail']} · 재생성 {v['regen']}회 · 최종 {'합격(재생성으로 살림)' if v['ok'] else '스킵'} · 파이프라인 계속 진행")
    with open(STORE / "verifier_test.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(KST).isoformat(timespec="seconds"), "fallback": {"verify_fail": st["verify_fail"],
                            "regen": v["regen"], "final_ok": v["ok"], "skipped": st["skipped"]}}, ensure_ascii=False) + "\n")
