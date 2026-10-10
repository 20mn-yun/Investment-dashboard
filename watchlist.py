"""통합 워치리스트 — watchlist.json 이 유일한 원본.

항목: {"market": "KR"|"US", "code": 6자리 종목코드|점 표기 티커, "name", "added_at"}
기존 설정 파일 3개(dart_monitor_config / calendar_watchlist / report_config)의
watchlist 부분은 sync_legacy() 가 통합본에서 한 방향으로 맞춘다.
  - DART 알림(dart_monitor_config.watchlist): KR 코드
  - 리포트 전달(report_config.watchlist): KR 종목명
  - 캘린더(calendar_watchlist.kr_earnings / us_earnings): KR 코드 / US 티커(야후 표기)
"""
import json
import os
import re
import tempfile
import threading
from datetime import datetime

_BD = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(_BD, "watchlist.json")
DART_CFG_FILE = os.path.join(_BD, "dart_monitor_config.json")
CAL_FILE = os.path.join(_BD, "calendar_watchlist.json")
REPORT_CFG_FILE = os.path.join(_BD, "report_config.json")
DART_CORP_MAP_FILE = os.path.join(_BD, "dart_corp_map.json")
US_STOCK_MAP_FILE = os.path.join(_BD, "us_stock_map.json")

MARKETS = ("KR", "US")
_lock = threading.RLock()


# ===== 미국 티커 표기 =====
# 화면과 watchlist.json 은 점 표기(BRK.B). 소스별 표기 변환은 여기서만 한다.
_US_SEP = {
    "display": ".",   # 화면, watchlist.json, us_stock_map.json(nasdaqtrader)
    "yahoo": "-",     # yfinance (캘린더 실적·배당 조회)
    "sec": "-",       # SEC company_tickers.json
}


def normalize_us_ticker(ticker):
    """BRK-B, BRK/B, brk.b → BRK.B"""
    t = (ticker or "").strip().upper()
    return re.sub(r"[-/]", ".", t)


def to_source_ticker(ticker, source):
    """점 표기 티커를 소스별 표기로. source: display | yahoo | sec"""
    return normalize_us_ticker(ticker).replace(".", _US_SEP[source])


def normalize_code(market, code):
    market = (market or "").strip().upper()
    if market == "KR":
        c = (code or "").strip().upper().replace(".KS", "").replace(".KQ", "")
        return c if re.fullmatch(r"\d{6}", c) else None
    if market == "US":
        c = normalize_us_ticker(code)
        return c if re.fullmatch(r"[A-Z0-9.]{1,10}", c) else None
    return None


# ===== 파일 입출력 =====
def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write_json(path, data):
    """같은 폴더 임시 파일에 쓴 뒤 os.replace 로 교체."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:   # mkstemp 는 0600 — 기존 파일 권한 유지 (새 파일은 0644)
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _lookup_name(market, code):
    if market == "KR":
        info = _read_json(DART_CORP_MAP_FILE, {}).get(code)
        if isinstance(info, dict):
            return info.get("name", "")
        return info or ""
    info = _read_json(US_STOCK_MAP_FILE, {}).get(code)
    return info.get("name", "") if isinstance(info, dict) else ""


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ===== 조회 =====
def load_watchlist():
    """[{market, code, name, added_at}, ...] (추가 순서)"""
    with _lock:
        data = _read_json(WATCHLIST_FILE, [])
        return data if isinstance(data, list) else []


def is_in_watchlist(market, code):
    m = (market or "").strip().upper()
    c = normalize_code(m, code)
    if not c:
        return False
    return any(i.get("market") == m and i.get("code") == c for i in load_watchlist())


def watchlist_keys():
    """{(market, code)} — 검색 결과 표시처럼 여러 번 조회할 때."""
    return {(i.get("market"), i.get("code")) for i in load_watchlist()}


# ===== 변경 =====
def _save(items):
    atomic_write_json(WATCHLIST_FILE, items)
    sync_legacy(items)


def add(market, code, name=None):
    """추가 후 세 파일 반영. 반환: (항목 또는 None, 새로 추가됐는지)"""
    m = (market or "").strip().upper()
    c = normalize_code(m, code)
    if not c:
        return None, False
    with _lock:
        items = load_watchlist()
        for i in items:
            if i.get("market") == m and i.get("code") == c:
                return i, False
        item = {"market": m, "code": c, "name": name or _lookup_name(m, c), "added_at": _now()}
        items.append(item)
        _save(items)
        return item, True


def remove(market, code):
    """삭제 후 세 파일 반영. 반환: 삭제됐는지"""
    m = (market or "").strip().upper()
    c = normalize_code(m, code)
    if not c:
        return False
    with _lock:
        items = load_watchlist()
        kept = [i for i in items if not (i.get("market") == m and i.get("code") == c)]
        if len(kept) == len(items):
            return False
        _save(kept)
        return True


def apply_changes(added=(), removed=(), names=None):
    """여러 건을 한 번에. added/removed: [(market, code)], names: {(market, code): name}"""
    names = names or {}
    with _lock:
        items = load_watchlist()
        keys = {(i["market"], i["code"]) for i in items}
        changed = False
        for market, code in removed:
            m = (market or "").upper()
            c = normalize_code(m, code)
            if (m, c) in keys:
                items = [i for i in items if not (i["market"] == m and i["code"] == c)]
                keys.discard((m, c))
                changed = True
        for market, code in added:
            m = (market or "").upper()
            c = normalize_code(m, code)
            if not c or (m, c) in keys:
                continue
            name = names.get((market, code)) or names.get((m, c)) or _lookup_name(m, c)
            items.append({"market": m, "code": c, "name": name, "added_at": _now()})
            keys.add((m, c))
            changed = True
        if changed:
            _save(items)
        return changed


# ===== 세 파일 반영 (통합본 → 기존 파일, 한 방향) =====
def sync_legacy(items=None):
    """세 파일의 watchlist 부분만 통합본에 맞춘다. 다른 키는 건드리지 않고, 바뀐 파일만 쓴다.
    반환: 실제로 쓴 파일 이름 목록"""
    with _lock:
        if items is None:
            items = load_watchlist()
        kr = [i for i in items if i.get("market") == "KR"]
        us = [i for i in items if i.get("market") == "US"]
        kr_codes = [i["code"] for i in kr]
        kr_names = list(dict.fromkeys(i["name"] for i in kr if i.get("name")))
        us_yahoo = [to_source_ticker(i["code"], "yahoo") for i in us]

        written = []
        dart = _read_json(DART_CFG_FILE, None)
        if isinstance(dart, dict) and dart.get("watchlist") != kr_codes:
            dart["watchlist"] = kr_codes
            atomic_write_json(DART_CFG_FILE, dart)
            written.append(os.path.basename(DART_CFG_FILE))

        cal = _read_json(CAL_FILE, None)
        if isinstance(cal, dict) and (cal.get("kr_earnings") != kr_codes or cal.get("us_earnings") != us_yahoo):
            cal["kr_earnings"] = kr_codes
            cal["us_earnings"] = us_yahoo
            atomic_write_json(CAL_FILE, cal)
            written.append(os.path.basename(CAL_FILE))

        rpt = _read_json(REPORT_CFG_FILE, None)
        if isinstance(rpt, dict) and rpt.get("watchlist") != kr_names:
            rpt["watchlist"] = kr_names
            atomic_write_json(REPORT_CFG_FILE, rpt)
            written.append(os.path.basename(REPORT_CFG_FILE))
        return written


# ===== 최초 1회 이관 =====
def migrate_once():
    """watchlist.json 이 없을 때만 세 파일의 합집합으로 만든다.
    반환: None(이미 있음) 또는 {"items": n, "unmatched": [이름...]}"""
    with _lock:
        if os.path.exists(WATCHLIST_FILE):
            return None
        corp = _read_json(DART_CORP_MAP_FILE, {})
        name_to_code = {}
        for code, info in corp.items():
            nm = info.get("name") if isinstance(info, dict) else info
            if nm:
                name_to_code.setdefault(nm, code)

        items, keys, unmatched = [], set(), []
        now = _now()

        def put(market, code, name=None):
            c = normalize_code(market, code)
            if not c or (market, c) in keys:
                return
            keys.add((market, c))
            items.append({"market": market, "code": c,
                          "name": name or _lookup_name(market, c), "added_at": now})

        dart = _read_json(DART_CFG_FILE, {}) or {}
        cal = _read_json(CAL_FILE, {}) or {}
        rpt = _read_json(REPORT_CFG_FILE, {}) or {}
        for c in dart.get("watchlist", []):
            put("KR", c)
        for c in cal.get("kr_earnings", []):
            put("KR", c)
        for nm in rpt.get("watchlist", []):
            code = name_to_code.get(nm)
            if code:
                put("KR", code, nm)
            else:
                unmatched.append(nm)
        for t in cal.get("us_earnings", []):
            put("US", t)

        atomic_write_json(WATCHLIST_FILE, items)
        if unmatched:
            # 버리지 않고 따로 보관 (리포트 전달 목록에서는 통합본 기준으로 빠진다)
            atomic_write_json(os.path.join(_BD, "watchlist_unmatched.json"),
                              {"migrated_at": now, "report_names": unmatched})
            print(f"[watchlist] 이관: 코드 대응 안 된 리포트 종목명 {unmatched}", flush=True)
        return {"items": len(items), "unmatched": unmatched}
