"""파이프라인을 한 번 실행한다.

  python run.py              # .env 의 DRY_RUN 설정을 따른다(기본: 보내지 않음)
  python run.py --dry-run    # 무조건 보내지 않고 store/last_payload.json 만 만든다
  python run.py --mock       # API 키 없이 배선만 시험(가짜 LLM, 발행 안 함)
  python run.py --hours 48   # 수집 기간을 48시간으로
"""
import os, sys

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--dry-run" in sys.argv:
        os.environ["DRY_RUN"] = "1"
    if "--mock" in sys.argv:
        os.environ["LLM_MOCK"] = "1"
    hours = int(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else None

    from graph import run              # 환경변수를 정한 뒤에 불러온다
    out = run(hours)
    for line in out["log"]:
        print(line)                    # 이 출력이 GitHub Actions 로그에 그대로 남는다
