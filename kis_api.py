import json
import os
import ssl
import time
import fcntl
import zipfile
import urllib.request
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_PATH = "cache/kis_token.json"

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")


def get_access_token():
    cache_path = Path(TOKEN_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = cache_path.with_suffix(".lock")
    lock_fd = open(lock_path, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            if cached["expires_at"] - time.time() > 3600:
                print("[KIS] Using cached token")
                return cached["access_token"]

        token_data = _request_new_token()
        if token_data is None:
            raise RuntimeError("Failed to obtain KIS access token")

        cache_path.write_text(json.dumps(token_data, indent=2))
        print("[KIS] New token issued and cached")
        return token_data["access_token"]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _request_new_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    resp = requests.post(url, json=body)

    if resp.status_code != 200 or "access_token" not in resp.json():
        error_text = resp.text
        if "EGW00133" in error_text:
            print("[KIS] Rate limited (EGW00133), waiting 60s before retry...")
            time.sleep(60)
            resp = requests.post(url, json=body)
            if resp.status_code != 200 or "access_token" not in resp.json():
                return None
        else:
            print(f"[KIS] Token request failed: {error_text}")
            return None

    data = resp.json()
    return {
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data["expires_in"]),
    }


def get_current_price(ticker_code):
    token = get_access_token()

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker_code,
    }

    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers,
        params=params,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Price query failed: {resp.text}")

    output = resp.json().get("output", {})

    return {
        "stck_prpr": int(output.get("stck_prpr", 0)),
        "stck_oprc": int(output.get("stck_oprc", 0)),
        "stck_hgpr": int(output.get("stck_hgpr", 0)),
        "stck_lwpr": int(output.get("stck_lwpr", 0)),
        "acml_vol": int(output.get("acml_vol", 0)),
        "acml_tr_pbmn": int(output.get("acml_tr_pbmn", 0)),
    }


_MST_CONFIG = {
    "KOSPI": {
        "url": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
        "zip_name": "kospi_code.zip",
        "mst_name": "kospi_code.mst",
        "part2_len": 228,
        "field_specs": [
            2, 1, 4, 4, 4,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 9, 5, 5, 1,
            1, 1, 2, 1, 1,
            1, 2, 2, 2, 3,
            1, 3, 12, 12, 8,
            15, 21, 2, 7, 1,
            1, 1, 1, 1, 9,
            9, 9, 5, 9, 8,
            9, 3, 1, 1, 1,
        ],
        "part2_columns": [
            "그룹코드", "시가총액규모", "지수업종대분류", "지수업종중분류", "지수업종소분류",
            "제조업", "저유동성", "지배구조지수종목", "KOSPI200섹터업종", "KOSPI100",
            "KOSPI50", "KRX", "ETP", "ELW발행", "KRX100",
            "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
            "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
            "Non1", "KRX증권", "KRX선박", "KRX섹터_보험", "KRX섹터_운송",
            "SRI", "기준가", "매매수량단위", "시간외수량단위", "거래정지",
            "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
            "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
            "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
            "상장주수", "자본금", "결산월", "공모가", "우선주",
            "공매도과열", "이상급등", "KRX300", "KOSPI", "매출액",
            "영업이익", "경상이익", "당기순이익", "ROE", "기준년월",
            "시가총액", "그룹사코드", "회사신용한도초과", "담보대출가능", "대주가능",
        ],
        "col_group": "그룹코드",
        "col_preferred": "우선주",
        "col_spac": "SPAC",
        "col_market_cap": "시가총액",
        "col_name": "한글명",
    },
    "KOSDAQ": {
        "url": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
        "zip_name": "kosdaq_code.zip",
        "mst_name": "kosdaq_code.mst",
        "part2_len": 222,
        "field_specs": [
            2, 1,
            4, 4, 4, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 1,
            1, 1, 1, 1, 9,
            5, 5, 1, 1, 1,
            2, 1, 1, 1, 2,
            2, 2, 3, 1, 3,
            12, 12, 8, 15, 21,
            2, 7, 1, 1, 1,
            1, 9, 9, 9, 5,
            9, 8, 9, 3, 1,
            1, 1,
        ],
        "part2_columns": [
            "증권그룹구분코드", "시가총액 규모 구분 코드 유가",
            "지수업종 대분류 코드", "지수 업종 중분류 코드", "지수업종 소분류 코드",
            "벤처기업 여부 (Y/N)", "저유동성종목 여부",
            "KRX 종목 여부", "ETP 상품구분코드", "KRX100 종목 여부 (Y/N)",
            "KRX 자동차 여부", "KRX 반도체 여부",
            "KRX 바이오 여부", "KRX 은행 여부", "기업인수목적회사여부",
            "KRX 에너지 화학 여부", "KRX 철강 여부",
            "단기과열종목구분코드", "KRX 미디어 통신 여부", "KRX 건설 여부",
            "(코스닥)투자주의환기종목여부", "KRX 증권 구분",
            "KRX 선박 구분", "KRX섹터지수 보험여부", "KRX섹터지수 운송여부",
            "KOSDAQ150지수여부 (Y,N)", "주식 기준가",
            "정규 시장 매매 수량 단위", "시간외 시장 매매 수량 단위",
            "거래정지 여부", "정리매매 여부", "관리 종목 여부",
            "시장 경고 구분 코드", "시장 경고위험 예고 여부", "불성실 공시 여부",
            "우회 상장 여부", "락구분 코드",
            "액면가 변경 구분 코드", "증자 구분 코드", "증거금 비율",
            "신용주문 가능 여부", "신용기간",
            "전일 거래량", "주식 액면가", "주식 상장 일자", "상장 주수(천)", "자본금",
            "결산 월", "공모 가격", "우선주 구분 코드",
            "공매도과열종목여부", "이상급등종목여부",
            "KRX300 종목 여부 (Y/N)", "매출액", "영업이익", "경상이익",
            "단기순이익", "ROE(자기자본이익률)",
            "기준년월", "전일기준 시가총액 (억)", "그룹사 코드",
            "회사신용한도초과여부", "담보대출가능여부", "대주가능여부",
        ],
        "col_group": "증권그룹구분코드",
        "col_preferred": "우선주 구분 코드",
        "col_spac": "기업인수목적회사여부",
        "col_market_cap": "전일기준 시가총액 (억)",
        "col_name": "한글종목명",
    },
}


def download_kr_stock_master(market):
    cfg = _MST_CONFIG[market]
    cache_dir = Path("cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{market.lower()}_master.pkl"

    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 86400:
        print(f"[KIS] Using cached {market} master file")
        return pd.read_pickle(cache_path)

    print(f"[KIS] Downloading {market} master file...")
    ssl._create_default_https_context = ssl._create_unverified_context

    zip_path = cache_dir / cfg["zip_name"]
    urllib.request.urlretrieve(cfg["url"], str(zip_path))

    with zipfile.ZipFile(str(zip_path)) as zf:
        zf.extractall(str(cache_dir))
    zip_path.unlink()

    mst_path = cache_dir / cfg["mst_name"]
    part1_rows = []
    part2_lines = []

    with open(str(mst_path), "r", encoding="cp949") as f:
        for row in f:
            rf1 = row[:len(row) - cfg["part2_len"]]
            part1_rows.append([rf1[0:9].rstrip(), rf1[9:21].rstrip(), rf1[21:].strip()])
            part2_lines.append(row[-cfg["part2_len"]:])

    mst_path.unlink()

    df1 = pd.DataFrame(part1_rows, columns=["단축코드", "표준코드", cfg["col_name"]])
    df2 = pd.read_fwf(
        StringIO("".join(part2_lines)),
        widths=cfg["field_specs"],
        names=cfg["part2_columns"],
    )

    df = pd.concat([df1, df2], axis=1)
    df.to_pickle(str(cache_path))
    print(f"[KIS] {market} master: {len(df)} entries cached")

    return df


def get_market_cap_ranking(market, top_n=300, min_market_cap_won=None):
    cfg = _MST_CONFIG[market]
    df = download_kr_stock_master(market)

    cap_col = cfg["col_market_cap"]
    df[cap_col] = pd.to_numeric(df[cap_col], errors="coerce").fillna(0)

    mask = (
        (df[cfg["col_group"]] == "ST")
        & (df[cfg["col_preferred"]] == 0)
        & (df[cfg["col_spac"]].astype(str) != "Y")
        & (df[cap_col] > 0)
    )
    df = df[mask].copy()

    if min_market_cap_won is not None:
        df = df[df[cap_col] * 100_000_000 >= min_market_cap_won]

    df = df.sort_values(cap_col, ascending=False).head(top_n)

    result = []
    for _, row in df.iterrows():
        result.append({
            "code": str(row["단축코드"]).zfill(6),
            "name": row[cfg["col_name"]],
            "market_cap": int(row[cap_col]) * 100_000_000,
            "market_sub": market,
        })

    return result


def _extract_ticker_code(ticker):
    base = ticker.split(".")[0]
    return base.strip().zfill(6)


def get_daily_price_history(ticker, period_days=90):
    if period_days > 100:
        raise ValueError(f"period_days={period_days} exceeds KIS API limit of 100 per call")

    code = _extract_ticker_code(ticker)
    token = get_access_token()

    from datetime import datetime, timedelta

    end_date = datetime.now()
    start_date = end_date - timedelta(days=int(period_days * 1.5))

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }

    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers=headers,
        params=params,
    )

    body = resp.json()
    if body.get("rt_cd") != "0":
        raise ValueError(f"KIS daily price failed for {code}: {body.get('msg1', resp.text)}")

    records = body.get("output2", [])
    if not records:
        return pd.Series(dtype=float, name=ticker)

    dates = []
    closes = []
    for rec in records:
        dt_str = rec.get("stck_bsop_date", "")
        close_str = rec.get("stck_clpr", "")
        if not dt_str or not close_str:
            continue
        dates.append(pd.Timestamp(dt_str))
        closes.append(float(close_str))

    series = pd.Series(closes, index=pd.DatetimeIndex(dates), name=ticker)
    series = series.sort_index()
    return series


if __name__ == "__main__":
    print("=== KIS API Test ===")
    print()

    print("[1] Token issuance test")
    try:
        token = get_access_token()
        print(f"    Token obtained: {token[:20]}...")
    except Exception as e:
        print(f"    Token failed: {e}")
        raise SystemExit(1)

    print()
    print("[2] Samsung Electronics (005930) current price")
    try:
        price = get_current_price("005930")
        print(f"    Current price : {price['stck_prpr']:,}")
        print(f"    Open          : {price['stck_oprc']:,}")
        print(f"    High          : {price['stck_hgpr']:,}")
        print(f"    Low           : {price['stck_lwpr']:,}")
        print(f"    Volume        : {price['acml_vol']:,}")
        print(f"    Trade value   : {price['acml_tr_pbmn']:,}")
    except Exception as e:
        print(f"    Price query failed: {e}")
        raise SystemExit(1)

    print()
    print("[3] Cached token reuse test")
    token2 = get_access_token()
    print(f"    Token reused: {token2[:20]}...")
    print(f"    Same token: {token == token2}")

    print()
    print("=== All tests passed ===")
