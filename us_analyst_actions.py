"""미국 종목 애널리스트 액션(투자의견·목표가 변경 이력) — yfinance upgrades_downgrades (C).

- refresh(code): 하루 1회 yf.Ticker(code).upgrades_downgrades → cache/analyst_actions/<티커>.json (fetched_at). 실패하면 기존 캐시 유지.
  batch_top_gainers 의 Yahoo 백오프(cache/yahoo_backoff.json)를 공유: 백오프 중이면 건너뛰고, 429 류 예외면 백오프를 건다.
- recent(code, days=90): [{date, firm, action, from_grade, to_grade, pt_action, prior_pt, current_pt}] 날짜 내림차순
한국 종목은 summaries.target_history 가 같은 역할을 하므로 대상 아님.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "analyst_actions")
_RATE_MARKERS = ("429", "too many requests", "rate limit", "ratelimit")


def _path(code):
    return os.path.join(CACHE_DIR, f"{watchlist_store.normalize_code('US', code) or code}.json")


def _load(code):
    try:
        with open(_path(code), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save(code, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(code))


def _backoff():
    """(백오프 중인지, until) — batch_top_gainers 의 공유 백오프 파일"""
    try:
        import batch_top_gainers as btg
        until = btg._yahoo_backoff_until()
        if until and until > datetime.now(timezone.utc):
            return True, until.isoformat()
    except Exception:
        pass
    return False, None


def _set_backoff(reason):
    try:
        import batch_top_gainers as btg
        btg._set_yahoo_backoff("us", f"analyst_actions: {reason}")
    except Exception:
        pass


def refresh(code, force=False):
    """하루 1회 갱신. 반환 {ok, rows, fetched_at, skipped|error}"""
    code = watchlist_store.normalize_code("US", code) or code
    cur = _load(code)
    today = date.today().isoformat()
    if not force and cur and (cur.get("fetched_at") or "")[:10] == today:
        return {"ok": True, "rows": len(cur.get("rows") or []), "fetched_at": cur.get("fetched_at"), "skipped": "오늘 갱신함"}
    busy, until = _backoff()
    if busy:
        return {"ok": False, "rows": len((cur or {}).get("rows") or []), "skipped": f"Yahoo 백오프 중({until})"}
    try:
        import yfinance as yf
        df = yf.Ticker(watchlist_store.to_source_ticker(code, "yahoo")).upgrades_downgrades
        rows = []
        if df is not None and len(df):
            df = df.reset_index()
            for _, r in df.iterrows():
                d = r.get("GradeDate")
                rows.append({"date": (d.isoformat(timespec="seconds") if hasattr(d, "isoformat") else str(d))[:19],
                             "firm": str(r.get("Firm") or ""), "action": str(r.get("Action") or ""),
                             "from_grade": str(r.get("FromGrade") or ""), "to_grade": str(r.get("ToGrade") or ""),
                             "pt_action": str(r.get("priceTargetAction") or ""),
                             "prior_pt": float(r["priorPriceTarget"]) if r.get("priorPriceTarget") not in (None, "") and float(r.get("priorPriceTarget") or 0) > 0 else None,
                             "current_pt": float(r["currentPriceTarget"]) if r.get("currentPriceTarget") not in (None, "") and float(r.get("currentPriceTarget") or 0) > 0 else None})
        rows.sort(key=lambda x: x["date"], reverse=True)
        data = {"market": "US", "code": code, "rows": rows, "fetched_at": datetime.now().isoformat(timespec="seconds"), "source": "yfinance upgrades_downgrades"}
        _save(code, data)
        return {"ok": True, "rows": len(rows), "fetched_at": data["fetched_at"]}
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        if any(k in msg.lower() for k in _RATE_MARKERS):
            _set_backoff(msg)
        print(f"[analyst_actions] {code} 갱신 실패(기존 캐시 유지): {msg}", flush=True)
        return {"ok": False, "rows": len((cur or {}).get("rows") or []), "error": msg}


def recent(code, days=90):
    """최근 days 일의 액션, 날짜 내림차순"""
    cur = _load(code) or {}
    since = (date.today() - timedelta(days=days)).isoformat()
    return [r for r in cur.get("rows") or [] if (r.get("date") or "")[:10] >= since]


def status(code, days=90):
    cur = _load(code) or {}
    return {"market": "US", "code": watchlist_store.normalize_code("US", code) or code, "fetched_at": cur.get("fetched_at"),
            "total_rows": len(cur.get("rows") or []), "days": days, "items": recent(code, days)}
