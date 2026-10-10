"""AI 호출 일일 한도 공통 함수.

쓰는 곳: 3-A 리포트 정리(stock_reports), 2단계 계정 대응(us_financials·kr_financials), 3-B 컨콜 요약·번역(earnings_calls).
규칙
  - kind 별로 하루 한도. 카운터는 cache/_ai_state.json 에 kind 별로 저장 ({"date": "YYYY-MM-DD", "counts": {kind: n}}).
  - take(kind, n=1, limit=None): limit 가 0 이면 "호출 금지"(항상 거절, 카운터 안 오름), None 이면 kind 의 기본값, 그 외는 그 값.
    허용되면 카운터를 n 만큼 올리고 (True, 오늘 사용량) 반환, 거절이면 (False, 오늘 사용량).
  - 날짜가 바뀌면 모든 카운터 초기화.
  - 3-A 때 "0 → 무제한" 버그가 있었으므로 0 과 None 을 구분하는 책임은 이 모듈이 진다 (resolve_limit).
"""
import json
import os
import threading
from datetime import date

_BD = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BD, "cache", "_ai_state.json")

DEFAULT_LIMITS = {
    "reports": 300,          # 3-A 증권사 리포트 정리
    "concept_map": 200,      # 2단계 계정 대응 (KR·US 공유)
    "call_summary": 20,      # 3-B 컨콜 요약
    "call_translate": 5,     # 3-B 컨콜 전문 번역 (녹취록 1건 = 1건)
    "peers": 50,             # 3-C 관련 기업 제안
}

_lock = threading.Lock()


def resolve_limit(kind, limit=None):
    """한도 값 해석: None → 기본값, 0 → 0(금지), 그 외 정수. 음수·이상한 값은 ValueError."""
    if limit is None:
        return DEFAULT_LIMITS.get(kind, 0)
    if isinstance(limit, bool):
        raise ValueError("한도는 정수여야 합니다")
    v = int(limit)
    if v < 0:
        raise ValueError("한도는 0 이상이어야 합니다")
    return v


def parse_limit_param(raw):
    """요청 파라미터(문자열·숫자·None) → None 또는 0 이상 정수. 빈 문자열은 None(기본값), '0' 은 0(금지)."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    return resolve_limit(None, raw)


def _load(path=None):
    try:
        with open(path or STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    today = date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "counts": {}}
    st.setdefault("counts", {})
    return st


def _save(st, path=None):
    path = path or STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def used(kind, path=None):
    with _lock:
        return _load(path)["counts"].get(kind, 0)


def take(kind, n=1, limit=None, path=None):
    """허용 여부 판단 + 카운터 증가. 반환 (허용 여부, 오늘 사용량)."""
    lim = resolve_limit(kind, limit)
    with _lock:
        st = _load(path)
        cur = st["counts"].get(kind, 0)
        if lim <= 0 or cur + n > lim:
            return False, cur
        st["counts"][kind] = cur + n
        _save(st, path)
        return True, cur + n


def snapshot(path=None):
    with _lock:
        st = _load(path)
        return {"date": st["date"], "counts": dict(st["counts"]),
                "limits": dict(DEFAULT_LIMITS)}


def self_test(verbose=True):
    """단위 시험: 한도 0 거절, 한도 2 는 세 번째 거절, 날짜 바뀌면 초기화. 실제 상태 파일은 건드리지 않는다."""
    import tempfile
    results = []
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "s.json")
        ok, n = take("t", limit=0, path=p)
        results.append(("한도 0 → 거절, 카운터 0", (not ok) and n == 0))
        ok1, _ = take("t", limit=2, path=p)
        ok2, _ = take("t", limit=2, path=p)
        ok3, n3 = take("t", limit=2, path=p)
        results.append(("한도 2 → 1·2번째 허용, 3번째 거절(사용량 2)", ok1 and ok2 and (not ok3) and n3 == 2))
        results.append(("None → 기본값 사용", resolve_limit("call_summary", None) == DEFAULT_LIMITS["call_summary"]))
        results.append(("'0' 문자열 → 0, '' → None", parse_limit_param("0") == 0 and parse_limit_param("") is None and parse_limit_param(None) is None))
        # 날짜 초기화: 상태 파일의 날짜를 어제로 바꿔 두고 다시 읽으면 0
        st = _load(p)
        st["date"] = "2000-01-01"
        _save(st, p)
        results.append(("날짜 바뀌면 초기화", used("t", path=p) == 0))
        ok4, n4 = take("t", limit=2, path=p)
        results.append(("초기화 뒤 다시 허용(사용량 1)", ok4 and n4 == 1))
    if verbose:
        for name, passed in results:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    return all(p for _, p in results)


if __name__ == "__main__":
    import sys
    sys.exit(0 if self_test() else 1)
