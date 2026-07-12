"""한국 수출 데이터 수집 모듈 (관세청 품목별 수출입실적 API + MTI-HSK 연계표).

- API: https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList (HS 10단위 조회)
- 연계표: 2026 MTI-HSK 코드표_vFF_260507.xlsx "HSK-MTI 연계표" 시트
- 응답은 국가x월 레코드이므로 월 단위로 합산한다 (총계 레코드는 제외).

CLI:
  python3 korea_export.py --backfill --items=반도체 --from=202201 [--to=202606]
  python3 korea_export.py --update
"""

import os
import json
import time
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("DATA_GO_KR_API_KEY")
API_URL = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XLSX_PATH = os.path.join(BASE_DIR, "2026 MTI-HSK 코드표_vFF_260507.xlsx")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
MAP_PATH = os.path.join(CACHE_DIR, "mti_hs_map.json")
DATA_PATH = os.path.join(CACHE_DIR, "korea_export.json")
PROGRESS_PATH = os.path.join(CACHE_DIR, "korea_export_progress.json")

CALL_INTERVAL = 0.3
RETRY_WAIT = 2
MAX_RETRY = 3

_call_counter = {"n": 0}


# ===== 연계표 로더 =====

def load_mti_hs_map():
    """{품목명(구분): {mti6: [hs10 목록]}} 매핑 + MTI 6단위 국문명.

    cache/mti_hs_map.json이 있으면 xlsx 재파싱 없이 JSON을 로드한다.
    반환: {"groups": {구분: {mti6: [hs10,...]}}, "mti_names": {mti6: 국문명}}
    """
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    import pandas as pd
    link = pd.read_excel(XLSX_PATH, sheet_name="HSK-MTI 연계표", header=0, dtype=str)
    mti_sheet = pd.read_excel(XLSX_PATH, sheet_name="MTI코드표", header=0, dtype=str)

    groups = {}
    for _, row in link.iterrows():
        group = row["구분"]
        mti6 = row["MTI"]
        hs10 = row["HSK"]
        groups.setdefault(group, {}).setdefault(mti6, []).append(hs10)
    for g in groups.values():
        for mti in g:
            g[mti] = sorted(set(g[mti]))

    mti_names = {}
    for _, row in mti_sheet.iterrows():
        code = row["MTI 코드"]
        if isinstance(code, str) and len(code) == 6:
            mti_names[code] = row["MTI 품목명(국문)"]

    result = {"groups": groups, "mti_names": mti_names}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result


# ===== 수집 함수 =====

def _month_chunks(start_yymm, end_yymm, max_months=12):
    """조회기간 1년 이내 API 제약에 맞춰 [start, end]를 12개월 단위 구간으로 나눈다."""
    chunks = []
    y, m = int(start_yymm[:4]), int(start_yymm[4:])
    ey, em = int(end_yymm[:4]), int(end_yymm[4:])
    while (y, m) <= (ey, em):
        idx = y * 12 + (m - 1) + (max_months - 1)
        cy, cm = idx // 12, idx % 12 + 1
        if (cy, cm) > (ey, em):
            cy, cm = ey, em
        chunks.append((f"{y}{m:02d}", f"{cy}{cm:02d}"))
        nxt = cy * 12 + (cm - 1) + 1
        y, m = nxt // 12, nxt % 12 + 1
    return chunks


def fetch_hs_monthly(hs10, start_yymm, end_yymm):
    """HS 10단위 코드 1개의 기간 내 월별 수출액(USD) 반환: {yyyymm: exp_dlr}.

    API가 1회 조회기간을 1년 이내로 제한하므로 12개월 구간으로 나눠 호출해 병합한다.
    국가별 레코드를 월 단위로 합산한다. 총계 레코드(year=총계)는 제외.
    호출 간 0.3초 간격, 실패 시 2초 대기 후 최대 3회 재시도.
    """
    monthly = {}
    for cs, ce in _month_chunks(start_yymm, end_yymm):
        part = _fetch_hs_monthly_once(hs10, cs, ce)
        for mth, v in part.items():
            monthly[mth] = monthly.get(mth, 0) + v
    return monthly


def _fetch_hs_monthly_once(hs10, start_yymm, end_yymm):
    for attempt in range(MAX_RETRY):
        try:
            _call_counter["n"] += 1
            res = requests.get(
                API_URL,
                params={
                    "serviceKey": API_KEY,
                    "strtYymm": start_yymm,
                    "endYymm": end_yymm,
                    "hsSgn": hs10,
                },
                timeout=60,
            )
            if res.status_code != 200:
                print(f"[korea_export] HTTP {res.status_code} hs={hs10} 시도={attempt+1}/{MAX_RETRY}", flush=True)
                time.sleep(RETRY_WAIT)
                continue
            root = ET.fromstring(res.content)
            code = root.findtext("./header/resultCode")
            if code != "00":
                msg = root.findtext("./header/resultMsg")
                print(f"[korea_export] resultCode={code} ({msg}) hs={hs10} 시도={attempt+1}/{MAX_RETRY}", flush=True)
                time.sleep(RETRY_WAIT)
                continue
            monthly = {}
            for item in root.iterfind("./body/items/item"):
                year = item.findtext("year") or ""
                if "." not in year:
                    continue
                yyyymm = year.replace(".", "")
                exp = int(item.findtext("expDlr") or 0)
                monthly[yyyymm] = monthly.get(yyyymm, 0) + exp
            time.sleep(CALL_INTERVAL)
            return monthly
        except Exception as e:
            print(f"[korea_export] 예외 {type(e).__name__}: {e} hs={hs10} 시도={attempt+1}/{MAX_RETRY}", flush=True)
            time.sleep(RETRY_WAIT)
    raise RuntimeError(f"HS {hs10} 수집 실패 ({MAX_RETRY}회 시도)")


# ===== 진행 체크포인트 =====

def _load_progress(params):
    if not os.path.exists(PROGRESS_PATH):
        return {"params": params, "done": {}}
    with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
        prog = json.load(f)
    if prog.get("params") != params:
        return {"params": params, "done": {}}
    return prog


def _save_progress(prog):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False)


# ===== 캐시 병합 =====

def _load_cache():
    if not os.path.exists(DATA_PATH):
        return {"meta": {}, "series": {}}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _merge_series(cache, mti6, name, group, monthly):
    s = cache["series"].setdefault(mti6, {"name": name, "group": group, "monthly": {}})
    s["name"] = name
    s["group"] = group
    s["monthly"].update({m: v for m, v in sorted(monthly.items())})


# ===== 백필 =====

def backfill(items, start_yymm, end_yymm=None, max_calls=None):
    """품목명(구분) 목록의 소속 HS 코드 전체를 수집해 MTI 6단위로 합산 저장.

    HS 코드 단위 체크포인트(cache/korea_export_progress.json)로 중단 후 이어받기 지원.
    items가 비어 있으면 '기타'를 제외한 20대 품목 전체를 대상으로 한다.
    max_calls 지정 시 재시도 포함 API 호출 수가 예산에 도달하면 진행 중인 HS 코드까지
    완료한 뒤 체크포인트를 저장하고 정상 종료한다 (캐시 병합은 전체 완료 시에만).
    """
    if not API_KEY:
        raise SystemExit("DATA_GO_KR_API_KEY가 .env에 없습니다.")
    if end_yymm is None:
        now = datetime.now()
        prev_year, prev_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        end_yymm = f"{prev_year}{prev_month:02d}"

    m = load_mti_hs_map()
    groups, mti_names = m["groups"], m["mti_names"]

    if not items:
        items = sorted(g for g in groups if g != "기타")
    targets = {}
    for item in items:
        if item not in groups:
            raise SystemExit(f"연계표에 없는 품목명: {item} (가능: {', '.join(sorted(groups))})")
        targets[item] = groups[item]

    all_hs = [(g, mti, hs) for g, mtis in targets.items() for mti, hss in mtis.items() for hs in hss]
    n_chunks = len(_month_chunks(start_yymm, end_yymm))
    print(f"[korea_export] 백필 대상: {', '.join(targets)} / HS 코드 {len(all_hs)}개 / 기간 {start_yymm}~{end_yymm}")
    print(f"[korea_export] 예상 호출 수: {len(all_hs) * n_chunks}회 (코드 {len(all_hs)}개 x 12개월 구간 {n_chunks}개)"
          + (f" / 이번 실행 예산 {max_calls}회" if max_calls else ""), flush=True)

    params = {"from": start_yymm, "to": end_yymm}
    prog = _load_progress(params)
    done = prog["done"]
    skip = sum(1 for _, _, hs in all_hs if hs in done)
    if skip:
        print(f"[korea_export] 체크포인트에서 {skip}개 코드 이어받음", flush=True)

    _call_counter["n"] = 0
    budget_hit = False
    t0 = time.time()
    for i, (g, mti, hs) in enumerate(all_hs, 1):
        if hs in done:
            continue
        if max_calls and _call_counter["n"] >= max_calls:
            budget_hit = True
            break
        monthly = fetch_hs_monthly(hs, start_yymm, end_yymm)
        done[hs] = monthly
        _save_progress(prog)
        if i % 10 == 0 or i == len(all_hs):
            print(f"[korea_export] {i}/{len(all_hs)} 수집 완료 (호출 {_call_counter['n']}회, {time.time()-t0:.0f}s)", flush=True)

    if budget_hit:
        done_cnt = sum(1 for _, _, hs in all_hs if hs in done)
        print(f"[korea_export] 예산 소진: {_call_counter['n']}회 호출, 진행률 {done_cnt}/{len(all_hs)} 코드, "
              f"내일 같은 명령으로 재개", flush=True)
        return

    cache = _load_cache()
    for g, mtis in targets.items():
        for mti, hss in mtis.items():
            agg = {}
            for hs in hss:
                for mth, v in done.get(hs, {}).items():
                    agg[mth] = agg.get(mth, 0) + v
            if not agg and mti in cache["series"]:
                continue
            _merge_series(cache, mti, mti_names.get(mti, mti), g, agg)

    meta = cache.setdefault("meta", {})
    meta["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rng = meta.setdefault("range", {})
    rng["from"] = min(rng.get("from", start_yymm), start_yymm)
    rng["to"] = max(rng.get("to", end_yymm), end_yymm)
    meta["items"] = sorted(set(meta.get("items", [])) | set(targets))
    _save_cache(cache)

    if os.path.exists(PROGRESS_PATH):
        os.remove(PROGRESS_PATH)
    print(f"[korea_export] 백필 완료: {DATA_PATH} (총 {time.time()-t0:.0f}s)", flush=True)


# ===== 증분 업데이트 =====

def update():
    """최근 2개월만 재수집해 기존 캐시에 병합한다."""
    if not API_KEY:
        raise SystemExit("DATA_GO_KR_API_KEY가 .env에 없습니다.")
    cache = _load_cache()
    if not cache["series"]:
        raise SystemExit("캐시가 비어 있습니다. 먼저 --backfill을 실행하세요.")

    now = datetime.now()
    prev_year, prev_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    if prev_month > 1:
        start_yymm = f"{prev_year}{prev_month-1:02d}"
    else:
        start_yymm = f"{prev_year-1}12"
    end_yymm = f"{prev_year}{prev_month:02d}"

    m = load_mti_hs_map()
    groups, mti_names = m["groups"], m["mti_names"]
    cached_items = cache.get("meta", {}).get("items", [])
    all_hs = [(g, mti, hs) for g in cached_items for mti, hss in groups.get(g, {}).items() for hs in hss]
    print(f"[korea_export] 증분 업데이트: {start_yymm}~{end_yymm} / HS 코드 {len(all_hs)}개 / 예상 호출 {len(all_hs)}회", flush=True)

    fetched = {}
    t0 = time.time()
    for i, (g, mti, hs) in enumerate(all_hs, 1):
        fetched[hs] = fetch_hs_monthly(hs, start_yymm, end_yymm)
        if i % 20 == 0 or i == len(all_hs):
            print(f"[korea_export] {i}/{len(all_hs)} ({time.time()-t0:.0f}s)", flush=True)

    for g in cached_items:
        for mti, hss in groups.get(g, {}).items():
            agg = {}
            for hs in hss:
                for mth, v in fetched.get(hs, {}).items():
                    agg[mth] = agg.get(mth, 0) + v
            if mti in cache["series"]:
                cache["series"][mti]["monthly"].update(agg)
            else:
                _merge_series(cache, mti, mti_names.get(mti, mti), g, agg)

    meta = cache["meta"]
    meta["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["range"]["to"] = max(meta["range"].get("to", end_yymm), end_yymm)
    _save_cache(cache)
    print(f"[korea_export] 증분 완료: {DATA_PATH} (총 {time.time()-t0:.0f}s)", flush=True)


# ===== 지표 계산 =====

def _prev_month(yyyymm):
    y, m = int(yyyymm[:4]), int(yyyymm[4:])
    return f"{y-1}12" if m == 1 else f"{y}{m-1:02d}"


def compute_indicators(monthly):
    """월별 수출액 {yyyymm: USD}에서 지표 계산.

    반환: {yyyymm: {"yoy": %, "delta_yoy": %p, "ma12": USD, "ttm": USD}}
    - yoy: (당월/전년동월 - 1) * 100, 전년동월 없으면 None
    - delta_yoy: 당월 yoy - 전월 yoy, 어느 한쪽이 None이면 None
    - ma12 / ttm: 당월 포함 직전 12개월 평균/합계, 12개월 미만이면 None
    """
    months = sorted(monthly)
    yoy = {}
    for m in months:
        prev_yr = f"{int(m[:4])-1}{m[4:]}"
        base = monthly.get(prev_yr)
        yoy[m] = (monthly[m] / base - 1) * 100 if base else None

    out = {}
    for i, m in enumerate(months):
        pm = _prev_month(m)
        d = None
        if yoy.get(m) is not None and yoy.get(pm) is not None:
            d = yoy[m] - yoy[pm]
        window = months[max(0, i - 11):i + 1]
        if len(window) == 12:
            total = sum(monthly[w] for w in window)
            ma12, ttm = total / 12, total
        else:
            ma12, ttm = None, None
        out[m] = {"yoy": yoy[m], "delta_yoy": d, "ma12": ma12, "ttm": ttm}

    for i, m in enumerate(months):
        w3 = months[max(0, i - 2):i + 1]
        ys = [out[w]["yoy"] for w in w3]
        ds = [out[w]["delta_yoy"] for w in w3]
        out[m]["m3_avg_yoy"] = sum(ys) / 3 if len(ys) == 3 and all(v is not None for v in ys) else None
        out[m]["m3_avg_delta"] = sum(ds) / 3 if len(ds) == 3 and all(v is not None for v in ds) else None
        ma12 = out[m]["ma12"]
        out[m]["ma_gap"] = (monthly[m] / ma12 - 1) * 100 if ma12 else None
        prev_yr = f"{int(m[:4])-1}{m[4:]}"
        cur_ttm = out[m]["ttm"]
        prev_ttm = out.get(prev_yr, {}).get("ttm")
        out[m]["ttm_yoy"] = (cur_ttm / prev_ttm - 1) * 100 if cur_ttm and prev_ttm else None
    return out


def build_comment(indicators):
    """최신월 지표 기반 한 줄 해설 생성 (규칙 기반)."""
    months = sorted(m for m, v in indicators.items() if v.get("yoy") is not None)
    if not months:
        return "지표를 계산할 데이터가 부족합니다."
    latest = months[-1]
    cur = indicators[latest]
    yoy = cur["yoy"]
    delta = cur["delta_yoy"]
    m3 = cur["m3_avg_yoy"]

    prev3 = months[-4:-1]
    turnaround = (yoy > 0 and len(prev3) == 3
                  and all(indicators[m]["yoy"] < 0 for m in prev3))

    if turnaround:
        return f"수개월간의 감소세를 벗어나 전년 대비 +{yoy:.1f}%로 플러스 전환에 성공했습니다."
    if yoy >= 30 and m3 is not None and m3 >= 30:
        return f"전년 대비 +{yoy:.1f}%, 최근 3개월 평균 +{m3:.1f}%로 높은 성장률이 여러 달 이어지고 있습니다."
    if yoy > 0 and delta is not None and delta < 0:
        return f"전년 대비 +{yoy:.1f}%로 성장세는 유지되고 있으나 증가율의 가속은 전월보다 {delta:.1f}%p 꺾였습니다."
    if yoy < -10:
        return f"전년 대비 {yoy:.1f}%로 두 자릿수 감소가 이어지는 부진 구간입니다."
    if yoy >= 10:
        return f"전년 대비 +{yoy:.1f}%의 견조한 증가 흐름을 보이고 있습니다."
    if yoy >= 0:
        return f"전년 대비 +{yoy:.1f}%로 완만한 증가 흐름입니다."
    return f"전년 대비 {yoy:.1f}%로 소폭 감소했으나 부진 기준(-10%)보다는 양호합니다."


def classify(yoy_series):
    """최신월 기준 분류. 입력: {yyyymm: yoy(% 또는 None)}.

    우선순위: 턴어라운드 > 둔화 > 성장구간(초고성장/고성장/안정/부진).
    """
    months = sorted(k for k, v in yoy_series.items() if v is not None)
    if not months:
        return "데이터부족"
    latest = months[-1]
    cur = yoy_series[latest]

    prev3 = months[-4:-1]
    if cur > 0 and len(prev3) == 3 and all(yoy_series[m] < 0 for m in prev3):
        return "턴어라운드"

    if cur > 0 and len(months) >= 3:
        d1 = cur - yoy_series[months[-2]]
        d2 = yoy_series[months[-2]] - yoy_series[months[-3]]
        if d1 < 0 and d2 < 0:
            return "둔화"

    if cur >= 30:
        return "초고성장"
    if cur >= 10:
        return "고성장"
    if cur >= -10:
        return "안정"
    return "부진"


def badge_series(yoy_series):
    """각 월을 최신월로 간주해 classify를 적용한 월별 배지 시계열 반환: {yyyymm: 배지}.

    턴어라운드/둔화 판정도 각 시점 기준(그 달까지의 데이터만 사용)으로 계산한다.
    """
    months = sorted(k for k, v in yoy_series.items() if v is not None)
    out = {}
    for i, m in enumerate(months):
        upto = {k: yoy_series[k] for k in months[:i + 1]}
        out[m] = classify(upto)
    return out


def badge_streak(yoy_series):
    """최신월과 같은 배지가 몇 개월 연속인지(최신월 포함) 반환."""
    series = badge_series(yoy_series)
    months = sorted(series)
    if not months:
        return 0
    latest_badge = series[months[-1]]
    streak = 0
    for m in reversed(months):
        if series[m] == latest_badge:
            streak += 1
        else:
            break
    return streak


# ===== CLI =====

def main():
    p = argparse.ArgumentParser(description="한국 수출 데이터 수집 (관세청 API + MTI-HSK 연계표)")
    p.add_argument("--backfill", action="store_true", help="지정 품목 백필")
    p.add_argument("--update", action="store_true", help="최근 2개월 증분 업데이트")
    p.add_argument("--items", type=str, default="", help="품목명 콤마 구분 (예: 반도체,자동차). 미지정 시 기타 제외 20대 품목 전체")
    p.add_argument("--from", dest="start", type=str, default="202201", help="시작 연월 YYYYMM")
    p.add_argument("--to", dest="end", type=str, default=None, help="종료 연월 YYYYMM (기본: 전월)")
    p.add_argument("--max-calls", dest="max_calls", type=int, default=None, help="이번 실행의 API 호출 예산 (재시도 포함)")
    args = p.parse_args()

    if args.backfill:
        items = [s.strip() for s in args.items.split(",") if s.strip()]
        backfill(items, args.start, args.end, max_calls=args.max_calls)
    elif args.update:
        update()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
