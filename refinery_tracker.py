"""미국 정유 지표 트래커 (EIA API v2).

VLO/MPC/PSX 포지션 조기경보용 주간 지표 3종:
  1) 미국 3-2-1 크랙스프레드 = (2*휘발유*42 + 1*ULSD*42)/3 - WTI
  2) ULSD-Brent 크랙 (유럽 한계설비 프록시) = ULSD*42 - Brent
  3) 유분(디젤)·휘발유 재고의 5년 계절 밴드 이탈도 z-score

- API: https://api.eia.gov/v2/seriesid/{id} (키: .env EIA_API_KEY)
- 캐시: cache/refinery.json (TTL 6시간)

CLI:
  python3 refinery_tracker.py            # 최신값·판정 JSON 출력
  python3 refinery_tracker.py --refresh  # 캐시 무시 재수집
"""

import os
import json
import time
import math
from datetime import datetime, date

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("EIA_API_KEY")
API_URL = "https://api.eia.gov/v2/seriesid/{sid}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "refinery.json")
CACHE_TTL = 6 * 3600

HISTORY_WEEKS = 156      # 차트용 3년
FETCH_LENGTH = 520       # z-score 5년 룩백 확보용 10년 수집
Z_LOOKBACK_YEARS = 5
Z_MIN_SAMPLES = 5

# 주간 시리즈 6개 (모두 실호출 검증 완료).
# 휘발유: NY하버 RBOB 주간 스팟(EER_EPMRR_PF4_Y35NY_DPG.W)은 EIA v2에 존재하지 않아
# NY하버 Conventional Regular 스팟으로 대체.
SERIES = {
    "wti": "PET.RWTC.W",                          # WTI 스팟 $/bbl
    "brent": "PET.RBRTE.W",                       # Brent 스팟 $/bbl
    "gasoline": "PET.EER_EPMRU_PF4_Y35NY_DPG.W",  # NY하버 휘발유 스팟 $/gal
    "ulsd": "PET.EER_EPD2DXL0_PF4_Y35NY_DPG.W",   # NY하버 ULSD 스팟 $/gal
    "dist_stock": "PET.WDISTUS1.W",               # 미국 유분 재고 천배럴
    "gas_stock": "PET.WGTSTUS1.W",                # 미국 휘발유 재고 천배럴
}

CRACK_ALERT_FLOOR = 20.0   # 3-2-1 크랙 $/bbl 경보선


# ===== 수집 =====

def _fetch_series(sid):
    """{date: value} (date는 'YYYY-MM-DD'). 실패 시 예외."""
    r = requests.get(
        API_URL.format(sid=sid),
        params={"api_key": API_KEY, "length": FETCH_LENGTH},
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json().get("response", {}).get("data", [])
    out = {}
    for row in rows:
        v = row.get("value")
        if v is None:
            continue
        out[row["period"]] = float(v)
    if not out:
        raise RuntimeError("빈 응답")
    return out


def fetch_all():
    """6개 시리즈 수집. 일부 실패 시 실패한 시리즈명을 명시해 예외."""
    if not API_KEY:
        raise RuntimeError("EIA_API_KEY가 설정되지 않았습니다 (.env 확인)")
    data, failed = {}, []
    for name, sid in SERIES.items():
        try:
            data[name] = _fetch_series(sid)
        except Exception as e:
            failed.append(f"{name}({sid}): {e}")
    if failed:
        raise RuntimeError("EIA 시리즈 수집 실패 — " + " / ".join(failed))
    return data


# ===== 계산 =====

def _compute_cracks(raw):
    """{date: crack_321}, {date: ulsd_brent} (날짜 교집합 기준)."""
    crack_321 = {}
    for d in set(raw["wti"]) & set(raw["gasoline"]) & set(raw["ulsd"]):
        crack_321[d] = (2 * raw["gasoline"][d] * 42 + raw["ulsd"][d] * 42) / 3 - raw["wti"][d]
    ulsd_brent = {}
    for d in set(raw["ulsd"]) & set(raw["brent"]):
        ulsd_brent[d] = raw["ulsd"][d] * 42 - raw["brent"][d]
    return crack_321, ulsd_brent


def _week_of(dstr):
    y, w, _ = date.fromisoformat(dstr).isocalendar()
    return y, w


def _compute_stock_z(stock):
    """{date: z} — 직전 5년 같은 주차±1주 평균/표준편차 대비 이탈도.

    표본 5개 미만이거나 표준편차 0이면 None (초기 5년 구간은 자연히 None).
    """
    weeks = {d: _week_of(d) for d in stock}
    out = {}
    for d, val in stock.items():
        y, w = weeks[d]
        samples = []
        for d2, (y2, w2) in weeks.items():
            if not (1 <= y - y2 <= Z_LOOKBACK_YEARS):
                continue
            dw = abs(w - w2)
            if min(dw, 53 - dw) <= 1:
                samples.append(stock[d2])
        if len(samples) < Z_MIN_SAMPLES:
            out[d] = None
            continue
        mean = sum(samples) / len(samples)
        var = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
        std = math.sqrt(var)
        out[d] = round((val - mean) / std, 2) if std > 0 else None
    return out


def _last_values(series_dict, n):
    """날짜 오름차순 정렬 후 마지막 n개 (date, value) 리스트."""
    items = sorted(series_dict.items())
    return items[-n:]


# ===== 판정 =====

def _judge_crack_321(values):
    """values: 오름차순 값 리스트 (None 제외)."""
    v = values[-1]
    if v < CRACK_ALERT_FLOOR:
        return "alert", f"${CRACK_ALERT_FLOOR:.0f}/bbl 하회 — 마진 급락"
    if len(values) >= 4 and all(values[i] < values[i - 1] for i in range(-3, 0)):
        return "warn", "최근 4주 하락 추세"
    return "ok", "정상 범위"


def _judge_ulsd_brent(values):
    if len(values) >= 5:
        diffs = [values[i] - values[i - 1] for i in range(-4, 0)]
        if all(d < 0 for d in diffs):
            return "alert", "4주 연속 하락 — 유럽 마진 축소"
        if values[-1] < values[-5]:
            return "warn", "4주 전 대비 하락 추세"
    return "ok", "정상 범위"


def _judge_dist_z(z):
    if z is None:
        return "warn", "z-score 계산 불가 (표본 부족)"
    if z > -1:
        return "warn", "5년 밴드 복귀 — 완충 재건 (고마진 국면 종료 신호)"
    return "ok", "재고 타이트 (5년 밴드 하단)"


def _judge_gas_z(z):
    if z is None:
        return "warn", "z-score 계산 불가 (표본 부족)"
    if z > 1:
        return "warn", "5년 밴드 상단 초과 — 휘발유 공급 과잉"
    if z <= -2:
        # 경보 아님(ok 유지) — 강세 정보를 화면에 드러내기 위한 메시지 분기
        return "ok", f"z={z} — 휘발유 재고 고갈 심화(차기 휘발유 크랙 강세 신호)"
    return "ok", "정상 범위"


def _judge_overall(latest):
    alerts = sum(1 for m in latest.values() if m["status"] == "alert")
    warns = sum(1 for m in latest.values() if m["status"] == "warn")
    if alerts >= 2:
        return "alert", f"경보 {alerts}개 — 포지션 절반 축소 검토"
    if alerts == 1:
        return "warn", "경보 1개 — 추가 악화 시 축소 검토"
    if warns:
        return "warn", f"주의 {warns}개 — 모니터링 강화"
    return "ok", "이상 없음 — 고마진 국면 유지"


# ===== 조립 =====

def build_data():
    raw = fetch_all()
    crack_321, ulsd_brent = _compute_cracks(raw)
    dist_z = _compute_stock_z(raw["dist_stock"])
    gas_z = _compute_stock_z(raw["gas_stock"])

    # 차트용 히스토리: 전체 날짜 합집합의 최근 156주, 없는 날짜는 null
    all_dates = sorted(set(crack_321) | set(ulsd_brent) | set(dist_z) | set(gas_z))
    dates = all_dates[-HISTORY_WEEKS:]

    def pick(series):
        return [series.get(d) for d in dates]

    history = {
        "dates": dates,
        "crack_321": [round(v, 2) if v is not None else None for v in pick(crack_321)],
        "ulsd_brent": [round(v, 2) if v is not None else None for v in pick(ulsd_brent)],
        "dist_z": pick(dist_z),
        "gas_z": pick(gas_z),
        "wti": pick(raw["wti"]),
        "brent": pick(raw["brent"]),
    }

    # 최신값·판정 (판정은 각 시리즈 자체 기준 최근값 사용)
    crack_vals = [v for _, v in _last_values(crack_321, 8)]
    ub_vals = [v for _, v in _last_values(ulsd_brent, 8)]
    if not crack_vals or not ub_vals:
        raise RuntimeError("크랙스프레드 계산 가능한 데이터가 없습니다")
    dist_items = _last_values(raw["dist_stock"], 1)
    gas_items = _last_values(raw["gas_stock"], 1)
    dist_z_latest = dist_z.get(dist_items[-1][0]) if dist_items else None
    gas_z_latest = gas_z.get(gas_items[-1][0]) if gas_items else None

    latest = {}
    st, msg = _judge_crack_321(crack_vals)
    latest["crack_321"] = {
        "value": round(crack_vals[-1], 2), "unit": "$/bbl",
        "date": _last_values(crack_321, 1)[-1][0], "status": st, "message": msg,
    }
    st, msg = _judge_ulsd_brent(ub_vals)
    latest["ulsd_brent"] = {
        "value": round(ub_vals[-1], 2), "unit": "$/bbl",
        "date": _last_values(ulsd_brent, 1)[-1][0], "status": st, "message": msg,
    }
    st, msg = _judge_dist_z(dist_z_latest)
    latest["dist_z"] = {
        "value": dist_z_latest, "unit": "σ",
        "stock_mbbl": dist_items[-1][1] if dist_items else None,
        "date": dist_items[-1][0] if dist_items else None, "status": st, "message": msg,
    }
    st, msg = _judge_gas_z(gas_z_latest)
    latest["gas_z"] = {
        "value": gas_z_latest, "unit": "σ",
        "stock_mbbl": gas_items[-1][1] if gas_items else None,
        "date": gas_items[-1][0] if gas_items else None, "status": st, "message": msg,
    }

    level, msg = _judge_overall(latest)
    data_as_of = max(m["date"] for m in latest.values() if m["date"])

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_as_of": data_as_of,
        "source_note": "EIA 주간 시리즈. 휘발유는 NY하버 Conventional Regular 스팟(RBOB 주간 스팟 부재로 대체).",
        "latest": latest,
        "overall": {"level": level, "message": msg},
        "history": history,
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


def get_refinery_data(force=False):
    """캐시(6시간 TTL) 경유 조회. force=True면 캐시 무시.

    재수집 실패 시 만료된 캐시라도 있으면 stale=True로 반환.
    """
    cached = _load_cached()
    if not force and cached and time.time() - cached.get("cached_at", 0) < CACHE_TTL:
        return cached["data"]
    try:
        data = build_data()
    except Exception:
        if cached:
            stale = dict(cached["data"])
            stale["stale"] = True
            return stale
        raise
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cached_at": time.time(), "data": data}, f, ensure_ascii=False)
    return data


if __name__ == "__main__":
    import sys
    force = "--refresh" in sys.argv
    result = get_refinery_data(force=force)
    out = {k: v for k, v in result.items() if k != "history"}
    out["history_weeks"] = len(result["history"]["dates"])
    print(json.dumps(out, ensure_ascii=False, indent=2))
