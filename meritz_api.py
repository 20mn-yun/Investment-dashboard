"""메리츠증권 Open API 조회 전용 클라이언트 (예수금·국내 잔고·해외 잔고·외화잔고).

인증: .env MERITZ_APP_KEY / MERITZ_APP_SECRET → POST /oauth2/token (client_credentials, scope=login).
토큰 캐시: cache/meritz_token.json (권한 600, 만료 1시간 전이면 재발급), kis_api 와 같은 파일 잠금.
계좌번호는 보내지 않는다(앱키에 묶인 계좌를 게이트웨이가 채움). 모든 함수는 조회(GET)만 하며,
HTTP 오류·응답 실패 코드는 MeritzAPIError 로 올린다.
"""
import fcntl
import json
import os
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

BASE_URL = "https://openapi.imeritz.com:9443"
APP_KEY = os.environ.get("MERITZ_APP_KEY")
APP_SECRET = os.environ.get("MERITZ_APP_SECRET")
TOKEN_CACHE_PATH = os.path.join(_BASE_DIR, "cache", "meritz_token.json")
TIMEOUT = 15
OK_CODES = {"5766", "0000", "00000"}
MAC = format(uuid.getnode(), "012X")


class MeritzAPIError(RuntimeError):
    pass


def _request_new_token():
    try:
        r = requests.post(f"{BASE_URL}/oauth2/token",
                          data={"grant_type": "client_credentials", "client_id": APP_KEY, "client_secret": APP_SECRET, "scope": "login"},
                          headers={"content-type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise MeritzAPIError(f"토큰 서버 연결 실패: {type(e).__name__}") from None
    try:
        d = r.json()
    except ValueError:
        raise MeritzAPIError(f"토큰 응답이 JSON 아님 (HTTP {r.status_code})") from None
    if r.status_code != 200 or "access_token" not in d:
        raise MeritzAPIError(f"토큰 발급 실패 HTTP {r.status_code} {d.get('rsp_cd') or d.get('error') or ''} {d.get('rsp_msg') or d.get('error_description') or ''}".strip())
    return {"access_token": d["access_token"], "expires_at": time.time() + int(d.get("expires_in", 43200))}


def get_access_token():
    if not APP_KEY or not APP_SECRET:
        raise MeritzAPIError("MERITZ_APP_KEY/MERITZ_APP_SECRET 환경변수가 없습니다")
    cache_path = Path(TOKEN_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(cache_path.with_suffix(".lock"), "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached["expires_at"] - time.time() > 3600:
                    return cached["access_token"]
            except (ValueError, KeyError):
                pass
        token_data = _request_new_token()
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token_data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, cache_path)
        return token_data["access_token"]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _get(path, params=None, max_pages=10):
    """GET 조회 1건(연속조회 포함). 반환 data (list 는 페이지를 이어 붙임, dict 는 그대로)."""
    headers = {"authorization": f"Bearer {get_access_token()}", "mac_address": MAC, "content-type": "application/json; charset=utf-8"}
    base = dict(params or {})
    p, rows, page = dict(base), [], 0
    while True:
        page += 1
        try:
            r = requests.get(BASE_URL + path, params=p, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise MeritzAPIError(f"{path} 연결 실패: {type(e).__name__}") from None
        try:
            b = r.json()
        except ValueError:
            raise MeritzAPIError(f"{path} 응답이 JSON 아님 (HTTP {r.status_code})") from None
        code = str(b.get("rsp_cd") or "")
        if r.status_code != 200 or code not in OK_CODES:
            msg = " ".join(x for x in ((b.get("rsp_msg") or "").strip(), (b.get("rsp_sub_msg") or "").strip()) if x)
            raise MeritzAPIError(f"{path} 실패 HTTP {r.status_code} rsp_cd={code} {msg}".strip())
        data = b.get("data")
        if isinstance(data, dict):
            return data
        if data is not None and not isinstance(data, list):
            raise MeritzAPIError(f"{path} data 형식 이상: {type(data).__name__}")
        rows += data or []
        if str(b.get("tr_cont") or "") in ("1", "Y", "M", "F") and b.get("tr_cont_key") and page < max_pages:
            p = dict(base, tr_cont="1", tr_cont_key=b["tr_cont_key"])
            time.sleep(0.2)
            continue
        return rows


def get_deposit():
    """예수금 상세 (tfam, d1_aft_tfam, d2_aft_tfam …). dict"""
    d = _get("/accounts/v1/me/deposit")
    if isinstance(d, list):
        d = d[0] if d else {}
    return d


def get_holdings():
    """국내 계좌 종목별 평가 (iscd, isnm, rmnd_qty, byng_unpr, mksc_cls_code …). list — 해외주식·채권도 섞여 온다"""
    return _get("/accounts/v1/me/holdings", {"nthn_rmnd_incu_yn": "N"})


def get_ovs_holdings():
    """해외주식 잔고 (ovrs_stck_symb_iscd, ovrs_exch_vndr_idno, rmnd_dqty, avrg_unpr, crcd …). list"""
    return _get("/accounts/v1/me/overseas/holdings")


def get_ovs_currencies():
    """해외 외화잔고 (crcd, frcr_amt, estm_frcr_amt …). list"""
    d = _get("/accounts/v1/me/overseas/currencies")
    return d if isinstance(d, list) else [d]
