"""정유 지표 유효성 검증 백테스트 (EIA 지표 vs VLO/MPC/PSX 주가).

refinery_tracker의 수집·계산 로직을 재사용해 2015년부터의 주간 시계열로
지표-주가 상관·예측력을 실증한다.

분석 4종 (종목별 × 지표별):
  1) 레벨 상관 (Pearson, 참고용)
  2) 13주 변화 상관 — 지표 diff(13) vs 주가 pct_change(13)  ← 핵심 테스트
  3) 선행-후행 — crack_321 13주 변화를 -8~+8주(2주 간격) shift, |상관| 최대 lag
  4) 예측력 IC — 현재 dist_z/gas_z vs 향후 13주 주가 수익률

판정:
  13주 변화 상관(|r| 기준): ≥0.4 유효 / 0.2~0.4 보조 / <0.2 재검토 필요
  IC(부호 기준): ≤-0.15 예측력 확인 / -0.15~0 약함 / >0 역방향(재검토)

캐시: cache/refinery_backtest.json (TTL 30일, 만료돼도 재계산은 refresh 요청 시에만)

CLI:
  python3 refinery_backtest.py            # 캐시 무시 전체 재계산 + 표 출력
"""

import os
import json
import time
from datetime import date, datetime

import pandas as pd

import refinery_tracker as rt

START = "2015-01-01"
TICKERS = ["VLO", "MPC", "PSX"]
INDICATORS = ["crack_321", "ulsd_brent", "dist_z", "gas_z"]
IND_LABELS = {
    "crack_321": "3-2-1 크랙", "ulsd_brent": "ULSD-Brent",
    "dist_z": "유분 z", "gas_z": "휘발유 z",
}

CACHE_PATH = os.path.join(rt.CACHE_DIR, "refinery_backtest.json")
CACHE_TTL = 30 * 24 * 3600

CHG_WEEKS = 13
ROLL_WEEKS = 104
LAGS = list(range(-8, 9, 2))


# ===== 데이터 구축 =====

def _fetch_length():
    return (date.today() - date.fromisoformat(START)).days // 7 + 12


def build_frame():
    """주간(W-FRI) 병합 DataFrame: 지표 4종 + 종목 3종 종가. 실패 티커 목록 동반."""
    n = _fetch_length()
    raw, failed = {}, []
    for name, sid in rt.SERIES.items():
        try:
            raw[name] = rt._fetch_series(sid, length=n)
        except Exception as e:
            failed.append(f"{name}({sid}): {e}")
    if failed:
        raise RuntimeError("EIA 시리즈 수집 실패 — " + " / ".join(failed))

    crack_321, ulsd_brent = rt._compute_cracks(raw)
    ind_df = pd.DataFrame({
        "crack_321": pd.Series(crack_321),
        "ulsd_brent": pd.Series(ulsd_brent),
        "dist_z": pd.Series(rt._compute_stock_z(raw["dist_stock"]), dtype=float),
        "gas_z": pd.Series(rt._compute_stock_z(raw["gas_stock"]), dtype=float),
    })
    ind_df.index = pd.to_datetime(ind_df.index)
    ind_df = ind_df.sort_index().resample("W-FRI").last()

    import yfinance as yf
    px = yf.download(TICKERS, start=START, interval="1d",
                     auto_adjust=True, progress=False)["Close"]
    failed_tickers = [t for t in TICKERS if t not in px.columns or px[t].dropna().empty]
    pxw = px.resample("W-FRI").last()

    df = ind_df.join(pxw, how="inner")
    df = df[df.index >= START]
    return df, failed_tickers


# ===== 판정 =====

def judge_chg(c):
    if c is None:
        return "계산 불가"
    a = abs(c)
    if a >= 0.4:
        return "유효"
    if a >= 0.2:
        return "보조"
    return "재검토 필요"


def judge_ic(c):
    if c is None:
        return "계산 불가"
    if c <= -0.15:
        return "예측력 확인"
    if c <= 0:
        return "약함"
    return "역방향(재검토)"


def _r(v):
    return None if v is None or pd.isna(v) else round(float(v), 3)


# ===== 분석 =====

def compute():
    t0 = time.time()
    df, failed_tickers = build_frame()
    tickers = [t for t in TICKERS if t not in failed_tickers]

    level, chg13, n_obs = {}, {}, {}
    for ind in INDICATORS:
        level[ind], chg13[ind], n_obs[ind] = {}, {}, {}
        ind_chg = df[ind].diff(CHG_WEEKS)
        for t in tickers:
            ret = df[t].pct_change(CHG_WEEKS, fill_method=None)
            level[ind][t] = _r(df[ind].corr(df[t]))
            c = _r(ind_chg.corr(ret))
            chg13[ind][t] = {"corr": c, "judgment": judge_chg(c)}
            n_obs[ind][t] = int(pd.concat([ind_chg, ret], axis=1).dropna().shape[0])

    # 선행-후행: crack_321 13주 변화 shift(k) vs 주가 13주 수익률 (k>0 = 지표 k주 선행)
    leadlag = {}
    crack_chg = df["crack_321"].diff(CHG_WEEKS)
    for t in tickers:
        ret = df[t].pct_change(CHG_WEEKS, fill_method=None)
        curve = {k: _r(crack_chg.shift(k).corr(ret)) for k in LAGS}
        best = max((k for k in LAGS if curve[k] is not None),
                   key=lambda k: abs(curve[k]))
        leadlag[t] = {"best_lag": best, "best_corr": curve[best], "curve": curve}

    # IC: 현재 z vs 향후 13주 수익률
    ic = {}
    for ind in ("dist_z", "gas_z"):
        ic[ind] = {}
        for t in tickers:
            fwd = df[t].pct_change(CHG_WEEKS, fill_method=None).shift(-CHG_WEEKS)
            c = _r(df[ind].corr(fwd))
            ic[ind][t] = {"corr": c, "judgment": judge_ic(c)}

    # 롤링 상관: crack_321 13주 변화 vs 3사 평균 주가 13주 수익률, 104주 창
    avg_ret = df[tickers].mean(axis=1).pct_change(CHG_WEEKS, fill_method=None)
    roll = avg_ret.rolling(ROLL_WEEKS).corr(crack_chg).dropna()
    rolling = {
        "dates": [d.strftime("%Y-%m-%d") for d in roll.index],
        "values": [round(float(v), 3) for v in roll.values],
    }

    elapsed = round(time.time() - t0, 1)
    return {
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": elapsed,
        "period": {"start": df.index[0].strftime("%Y-%m-%d"),
                   "end": df.index[-1].strftime("%Y-%m-%d")},
        "tickers": tickers,
        "failed_tickers": failed_tickers,
        "n_obs": n_obs,
        "chg_weeks": CHG_WEEKS,
        "roll_weeks": ROLL_WEEKS,
        "corr_level": level,
        "corr_chg13": chg13,
        "leadlag": leadlag,
        "ic": ic,
        "rolling": rolling,
        "notes": "13주 변화 상관 판정은 |상관| 기준(재고 z는 음의 상관이 정상 방향). IC 판정은 부호 기준.",
    }


# ===== 캐시 =====

def _load_cached():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_backtest_data(force=False):
    """force=True면 재계산(수 분 소요 가능). 아니면 캐시 반환(만료 시 stale 플래그).

    캐시가 없고 force도 아니면 None — 탭 로드시 장시간 자동 계산을 막기 위함.
    """
    cached = _load_cached()
    if not force:
        if not cached:
            return None
        data = cached["data"]
        if time.time() - cached.get("cached_at", 0) >= CACHE_TTL:
            data = dict(data)
            data["stale"] = True
        return data
    data = compute()
    os.makedirs(rt.CACHE_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cached_at": time.time(), "data": data}, f, ensure_ascii=False)
    return data


# ===== CLI =====

def _print_table(result):
    tickers = result["tickers"]
    print(f"\n분석 기간: {result['period']['start']} ~ {result['period']['end']}"
          f" | 계산 시간: {result['elapsed_sec']}초")
    if result["failed_tickers"]:
        print("yfinance 수집 실패:", ", ".join(result["failed_tickers"]))

    print("\n[1] 레벨 상관 (참고용)")
    print(f"{'지표':<12}" + "".join(f"{t:>8}" for t in tickers))
    for ind in INDICATORS:
        row = "".join(f"{result['corr_level'][ind][t]:>8}" for t in tickers)
        print(f"{IND_LABELS[ind]:<12}{row}")

    print(f"\n[2] 13주 변화 상관 (핵심, n={result['n_obs']['crack_321'][tickers[0]]})")
    for ind in INDICATORS:
        cells = [result["corr_chg13"][ind][t] for t in tickers]
        row = "".join(f"{c['corr']:>8}" for c in cells)
        print(f"{IND_LABELS[ind]:<12}{row}   {cells[0]['judgment']}/{cells[1]['judgment']}/{cells[2]['judgment']}"
              if len(cells) == 3 else f"{IND_LABELS[ind]:<12}{row}")

    print("\n[3] 선행-후행 (crack_321 13주 변화 shift, k>0 = 지표 k주 선행)")
    for t in tickers:
        ll = result["leadlag"][t]
        curve = " ".join(f"{k:+d}:{v}" for k, v in ll["curve"].items())
        print(f"{t}: 최적 lag {ll['best_lag']:+d}주 (r={ll['best_corr']}) | {curve}")

    print("\n[4] 예측력 IC (현재 z vs 향후 13주 수익률)")
    for ind in ("dist_z", "gas_z"):
        for t in tickers:
            e = result["ic"][ind][t]
            print(f"{IND_LABELS[ind]:<8} {t}: {e['corr']:>7}  {e['judgment']}")

    rd = result["rolling"]["dates"]
    print(f"\n롤링 상관({result['roll_weeks']}주, 3사 평균): {len(rd)}개 포인트"
          f" ({rd[0]} ~ {rd[-1]}), 최근값 {result['rolling']['values'][-1]}")


if __name__ == "__main__":
    result = get_backtest_data(force=True)
    _print_table(result)
