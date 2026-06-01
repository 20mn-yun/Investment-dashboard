import json
import os
import sys
import time
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "cache", "stock_quarterly_eps.json")
TTL_DAYS = 30
QUARTERS_PER_YEAR = [("Q1", "11013"), ("H1", "11012"), ("3Q", "11014")]
ANN_CODE = "11011"
EPS_EXCLUDE = ["우선주", "중단영업", "종류주식"]


def is_quarterly_earnings_season(d=None):
    if d is None:
        d = date.today()
    m, day = d.month, d.day
    if m == 4 or m == 5:
        return True, "Q1 (1분기 잠정실적+분기보고서: 4/1~5/31)"
    if (m == 7 and day >= 15) or m == 8:
        return True, "Q2 (2분기 잠정실적+반기보고서: 7/15~8/31)"
    if (m == 10 and day >= 15) or m == 11:
        return True, "Q3 (3분기 잠정실적+분기보고서: 10/15~11/30)"
    if m == 2 or m == 3:
        return True, "Q4 (사업보고서+Q4 잠정: 2/1~3/31)"
    return False, None


def _load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _match_eps_row(row_name):
    name = row_name.replace('　', ' ').strip()
    if any(k in name for k in EPS_EXCLUDE):
        return False
    if '희석' in name and '기본' not in name:
        return False
    has_jujang = '주당' in name
    has_profit = any(k in name for k in ['이익', '순이익', '손익', '손실'])
    return has_jujang and has_profit


def _extract_eps(data):
    if not data:
        return None
    for item in data.get("list", []):
        nm = item.get("account_nm", "")
        if not _match_eps_row(nm):
            continue
        val_str = (item.get("thstrm_amount") or "").replace(",", "").strip()
        if not val_str or val_str == "-":
            continue
        try:
            return int(val_str)
        except ValueError:
            try:
                return float(val_str)
            except ValueError:
                pass
    return None


def fetch_quarterly_eps(stock_code, max_years=2):
    sys.path.insert(0, BASE_DIR) if BASE_DIR not in sys.path else None
    from earnings_tracker import _fetch_fnltt_raw, get_corp_code

    corp_code = get_corp_code(stock_code)
    if not corp_code:
        return []

    current_year = date.today().year
    results = []

    for year in range(current_year, current_year - max_years - 1, -1):
        q1_eps = None
        q2_eps = None
        q3_eps = None

        for q_label, q_code in QUARTERS_PER_YEAR:
            data = _fetch_fnltt_raw(corp_code, year, q_code)
            eps = _extract_eps(data)
            if eps is None:
                continue
            q_num = {"Q1": 1, "H1": 2, "3Q": 3}[q_label]
            results.append({
                "period": f"{year}Q{q_num}",
                "year": year,
                "quarter": q_num,
                "eps": eps,
            })
            if q_num == 1:
                q1_eps = eps
            elif q_num == 2:
                q2_eps = eps
            elif q_num == 3:
                q3_eps = eps

        ann_data = _fetch_fnltt_raw(corp_code, year, ANN_CODE)
        ann_eps = _extract_eps(ann_data)
        if ann_eps is not None and q1_eps is not None and q2_eps is not None and q3_eps is not None:
            q4_eps = ann_eps - q1_eps - q2_eps - q3_eps
            results.append({
                "period": f"{year}Q4",
                "year": year,
                "quarter": 4,
                "eps": q4_eps,
            })

    results.sort(key=lambda x: (x["year"], x["quarter"]))
    return results


def compute_quarterly_trend(eps_series):
    if not eps_series or len(eps_series) < 2:
        return None

    latest = eps_series[-1]
    prev = eps_series[-2]

    qoq = None
    if prev["eps"] is not None and latest["eps"] is not None and prev["eps"] != 0:
        qoq = round((latest["eps"] - prev["eps"]) / abs(prev["eps"]) * 100, 2)

    yoy = None
    target_q = latest["quarter"]
    target_y = latest["year"] - 1
    for entry in eps_series:
        if entry["year"] == target_y and entry["quarter"] == target_q:
            if entry["eps"] is not None and entry["eps"] != 0:
                yoy = round((latest["eps"] - entry["eps"]) / abs(entry["eps"]) * 100, 2)
            break

    return {
        "latest_period": latest["period"],
        "latest_eps": latest["eps"],
        "qoq": qoq,
        "yoy": yoy,
    }


def get_quarterly_trend(stock_code):
    cache = _load_cache()
    entry = cache.get(stock_code)
    if entry:
        try:
            cached_at = date.fromisoformat(entry.get("cached_at", ""))
            if (date.today() - cached_at).days < TTL_DAYS:
                return entry.get("trend")
        except Exception:
            pass

    eps_series = fetch_quarterly_eps(stock_code)
    trend = compute_quarterly_trend(eps_series)

    cache[stock_code] = {
        "cached_at": date.today().isoformat(),
        "eps_series": eps_series,
        "trend": trend,
    }
    _save_cache(cache)
    return trend


def batch_fetch_all():
    sys.path.insert(0, BASE_DIR) if BASE_DIR not in sys.path else None
    from stock_leaders import load_stock_sector_mapping

    mapping = load_stock_sector_mapping()
    codes = list(mapping.keys())
    print(f"[quarterly_eps] {len(codes)} 종목 일괄 수집 시작")

    cache = _load_cache()
    today_str = date.today().isoformat()
    fetched = 0
    skipped = 0
    t0 = time.time()

    for i, code in enumerate(codes):
        entry = cache.get(code)
        if entry:
            try:
                cached_at = date.fromisoformat(entry.get("cached_at", ""))
                if (date.today() - cached_at).days < TTL_DAYS:
                    skipped += 1
                    continue
            except Exception:
                pass

        eps_series = fetch_quarterly_eps(code)
        trend = compute_quarterly_trend(eps_series)
        cache[code] = {
            "cached_at": today_str,
            "eps_series": eps_series,
            "trend": trend,
        }
        fetched += 1

        if fetched % 20 == 0:
            _save_cache(cache)
            elapsed = time.time() - t0
            print(f"  {i+1}/{len(codes)}: {fetched} fetched, {skipped} cached ({elapsed:.0f}s)")

    _save_cache(cache)
    elapsed = time.time() - t0
    with_data = sum(1 for v in cache.values() if v.get("trend"))
    print(f"[quarterly_eps] 완료: {fetched} fetched, {skipped} cached, {with_data}/{len(codes)} with data ({elapsed:.0f}s)")
    return cache


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 quarterly_eps_tracker.py <stock_code> | --all | --status | --season-check")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "--season-check":
        in_season, name = is_quarterly_earnings_season()
        today_str = date.today().isoformat()
        if in_season:
            print(f"[{today_str}] 시즌 ON: {name}")
        else:
            print(f"[{today_str}] 시즌 OFF: 분기 실적 발표 시즌 아님")
        sys.exit(0)

    if cmd == "--status":
        cache = _load_cache()
        with_trend = sum(1 for v in cache.values() if v.get("trend"))
        print(f"캐시 종목: {len(cache)}, 트렌드 있음: {with_trend}")
        for code in list(cache.keys())[:5]:
            t = cache[code].get("trend")
            if t:
                print(f"  {code}: {t['latest_period']} EPS={t['latest_eps']} QoQ={t['qoq']} YoY={t['yoy']}")
        sys.exit(0)

    if cmd == "--all":
        in_season, name = is_quarterly_earnings_season()
        if not in_season:
            print(f"[quarterly_eps] 분기 시즌 아님, skip ({date.today().isoformat()})")
            sys.exit(0)
        print(f"[quarterly_eps] 시즌 ON: {name}")
        batch_fetch_all()
        sys.exit(0)

    stock_code = cmd.replace(".KS", "").replace(".KQ", "")
    print(f"=== {stock_code} ===")
    t0 = time.time()
    eps_series = fetch_quarterly_eps(stock_code)
    trend = compute_quarterly_trend(eps_series)
    elapsed = time.time() - t0

    if eps_series:
        print(f"분기 EPS ({len(eps_series)}개):")
        for e in eps_series:
            print(f"  {e['period']}: {e['eps']}")
    else:
        print("분기 EPS 데이터 없음")

    if trend:
        print(f"\n최신: {trend['latest_period']} EPS={trend['latest_eps']}")
        print(f"QoQ: {trend['qoq']}%")
        print(f"YoY: {trend['yoy']}%")
    print(f"소요시간: {elapsed:.1f}초")
