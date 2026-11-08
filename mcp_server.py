"""
대시보드용 MCP 서버 (Google OAuth + 종목 도구 + 운영 진단).

- 외부 주소: https://mcp.20mn.app (Cloudflare 터널 → localhost:8100)
- 인증: FastMCP GoogleProvider(OAuth Proxy). 허용 계정 1개(MCP_ALLOWED_EMAIL).
- MCP 엔드포인트: /mcp  (claude.ai 등록 주소 = https://mcp.20mn.app/mcp)
- 로그: logs/mcp_server.log (비밀값 기록 금지)
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------------------------------------------------------- 로깅
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "mcp_server.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler(sys.stderr)])
log = logging.getLogger("mcp_server")

# ---------------------------------------------------------------- 환경변수
CLIENT_ID = os.getenv("MCP_GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("MCP_GOOGLE_CLIENT_SECRET", "").strip()
ALLOWED_EMAIL = os.getenv("MCP_ALLOWED_EMAIL", "").strip().lower()
BASE_URL = os.getenv("MCP_BASE_URL", "https://mcp.20mn.app").strip().rstrip("/")

_missing = [
    name
    for name, val in (
        ("MCP_GOOGLE_CLIENT_ID", CLIENT_ID),
        ("MCP_GOOGLE_CLIENT_SECRET", CLIENT_SECRET),
        ("MCP_ALLOWED_EMAIL", ALLOWED_EMAIL),
    )
    if not val
]
if _missing:
    log.error("필수 환경변수 누락: %s (.env에 추가 필요)", ", ".join(_missing))
    sys.exit(1)

# ---------------------------------------------------------------- 인증
import fastmcp  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.auth import AuthContext  # noqa: E402
from fastmcp.server.auth.providers.google import GoogleProvider  # noqa: E402
from fastmcp.server.dependencies import get_access_token  # noqa: E402
from fastmcp.server.middleware import AuthMiddleware  # noqa: E402

KST = timezone(timedelta(hours=9))
MCP_PATH = "/mcp"

auth = GoogleProvider(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    base_url=BASE_URL,
    required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
)


def _token_email(token) -> str | None:
    """검증된 AccessToken에서 Google 이메일을 꺼낸다. 없으면 None."""
    if token is None:
        return None
    claims = token.claims or {}
    email = claims.get("email") or (claims.get("upstream_claims") or {}).get("email")
    return email.strip().lower() if isinstance(email, str) and email else None


def allowed_email_only(ctx: AuthContext) -> bool:
    """전역 AuthCheck: 허용 이메일이 아니면 모든 도구/리소스/프롬프트 거부 (fail-closed)."""
    email = _token_email(ctx.token)
    ok = email is not None and email == ALLOWED_EMAIL
    if not ok:
        log.warning("접근 거부: component=%s email=%s", getattr(ctx.component, "name", "?"), email or "(none)")
    return ok


def require_allowed_email() -> str:
    """도구 내부 공통 가드(이중 방어). 허용 이메일이 아니면 예외."""
    email = _token_email(get_access_token())
    if email is None or email != ALLOWED_EMAIL:
        raise PermissionError("not allowed")
    return email


mcp = FastMCP(
    name="investment-dashboard",
    auth=auth,
    middleware=[AuthMiddleware(auth=allowed_email_only)],
)


@mcp.tool
def ping() -> dict:
    """서버 생존/인증 확인. 대시보드 데이터는 다른 도구(search_stock → get_stock_summary 등)를 쓴다."""
    email = require_allowed_email()
    return {
        "ok": True,
        "server_time": datetime.now(KST).isoformat(timespec="seconds"),
        "email": email,
        "version": fastmcp.__version__,
    }


# ====================================================================== 대시보드 Flask 연동 (Gate 1)
import json  # noqa: E402
import re  # noqa: E402
from datetime import date  # noqa: E402

import requests  # noqa: E402

DASH_BASE = os.getenv("MCP_DASH_BASE", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 60
FIN_MAX_BYTES = 40_000
NOTE_FIELDS = {"idea": "투자아이디어", "assumptions": "핵심가정", "followup": "Follow-up 포인트", "risks": "Risk 포인트"}
NOTE_FIELD_BY_LABEL = {v: k for k, v in NOTE_FIELDS.items()}
NOTE_FIELD_BY_LABEL.update({"투자 아이디어": "idea", "핵심 가정": "assumptions", "follow-up": "followup", "followup 포인트": "followup",
                            "risk": "risks", "risk 포인트": "risks", "리스크": "risks"})
# telegram_report.ALIASES 와 같은 약칭표 (telethon 의존을 피하려고 복사). 바뀌면 함께 고칠 것.
ALIASES = {"현대일렉트릭": "HD현대일렉트릭", "하이닉스": "SK하이닉스", "s-oil": "S-Oil", "lselectric": "엘에스일렉트릭",
           "현대차": "현대자동차", "samg엔터": "SAMG엔터", "brk": "BRK.B"}
_ALIAS_NORM = {re.sub(r"\s+", "", k.lower()): v for k, v in ALIASES.items()}


def _api(path, params=None, timeout=DEFAULT_TIMEOUT, method="GET", body=None):
    """Flask 호출. 반환 (data, err). err 는 {"ok": False, "error": ..., "message": ...} 또는 None. 예외를 던지지 않는다."""
    timeout = min(timeout, MAX_TIMEOUT)
    try:
        if method == "GET":
            r = requests.get(DASH_BASE + path, params=params, timeout=timeout)
        else:
            r = requests.request(method, DASH_BASE + path, params=params, json=body, timeout=timeout)
    except requests.Timeout:
        return None, {"ok": False, "error": "timeout", "message": f"대시보드 응답 없음 ({timeout}s): {path}"}
    except requests.RequestException as e:
        return None, {"ok": False, "error": "dashboard_unreachable", "message": f"{type(e).__name__}: {path}"}
    try:
        data = r.json()
    except ValueError:
        return None, {"ok": False, "error": "bad_response", "message": f"HTTP {r.status_code} (JSON 아님): {path}"}
    if r.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        msg = data.get("message") if isinstance(data, dict) else None
        return data, {"ok": False, "error": err or f"http_{r.status_code}", "message": msg or f"HTTP {r.status_code}: {path}", "status": r.status_code}
    return data, None


def _market(m):
    m = (m or "").strip().upper()
    return m if m in ("KR", "US") else None


def _code(market, code):
    c = (code or "").strip().upper()
    if market == "KR":
        c = c.replace(".KS", "").replace(".KQ", "")
        return c if re.fullmatch(r"\d{6}", c) else None
    c = c.replace("-", ".")
    return c if re.fullmatch(r"[A-Z0-9.]{1,10}", c) else None


def _target(market, code):
    """(market, code, err). 시장·코드 형식 검증."""
    m = _market(market)
    if not m:
        return None, None, {"ok": False, "error": "bad_market", "message": "market 은 'kr' 또는 'us'"}
    c = _code(m, code)
    if not c:
        return None, None, {"ok": False, "error": "bad_code", "message": "code 형식 오류 (한국 6자리 숫자, 미국 티커)"}
    return m, c, None


def _won(v):
    """원 → '1,131조 2,548억' (종목 탭 svWon 과 같은 표기)"""
    if v is None:
        return None
    sign = "-" if v < 0 else ""
    a = abs(v)
    eok = round(a / 1e8)
    if eok == 0:
        return "0" if a == 0 else f"{sign}{round(a / 1e4):,}만"
    jo, rest = divmod(eok, 10000)
    return f"{sign}{jo:,}조 {rest:,}억" if jo else f"{sign}{rest:,}억"


def _usd(v):
    """달러 → '$4.50T' / '$96.0B' / '$12.3M' (종목 탭 svUsd 와 같은 표기)"""
    if v is None:
        return None
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def _money(market, v):
    return _usd(v) if market == "US" else _won(v)


def _per_share(market, v):
    if v is None:
        return None
    return f"${v:.2f}" if market == "US" else f"{round(v):,}원"


def _shares(v):
    if v is None:
        return None
    return f"{v / 1e9:.1f}B주" if v >= 1e9 else f"{v / 1e6:.1f}M주" if v >= 1e6 else f"{round(v):,}주"


def _r(v, n=2):
    return None if v is None else round(v, n)


def _since(days):
    return (date.today() - timedelta(days=days)).isoformat()


def _trim(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- ① 검색
@mcp.tool
def search_stock(query: str) -> dict:
    """종목 검색: 이름·6자리 코드·미국 티커·약칭(하이닉스, 현대차 등) → market('kr'|'us') + code. 최대 10건.
    다른 도구를 부르기 전에 market/code 를 확정할 때 쓴다. 각 항목에 워치리스트 포함 여부(in_watchlist)가 있다."""
    require_allowed_email()
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "bad_query", "message": "검색어가 비어 있음"}
    alias = _ALIAS_NORM.get(re.sub(r"\s+", "", q.lower()))
    out, seen = [], set()
    for term in ([alias] if alias else []) + [q]:
        data, err = _api("/api/stock/search", {"q": term}, timeout=10)
        if err:
            return err
        for it in data or []:
            if it.get("wl_market") not in ("KR", "US"):
                continue   # 일본·유럽은 종목종합정보 탭 대상이 아님
            key = (it["wl_market"], it["wl_code"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"market": it["wl_market"].lower(), "code": it["wl_code"], "name": it.get("name"),
                        "in_watchlist": bool(it.get("in_watchlist"))})
    res = {"ok": True, "query": q, "count": min(len(out), 10), "results": out[:10]}
    if alias:
        res["alias"] = f"{q} → {alias}"
    if not out:
        res["hint"] = "결과 없음. 미국 종목은 영문 회사명 또는 티커로, 한국 종목은 한글명 또는 6자리 코드로 검색"
    return res


# ---------------------------------------------------------------- ② 요약
def _disclosure_items(market, code, days, limit=None):
    """공시 목록(날짜 필터·한 줄 요약). (items, err). KR 은 저장본 30분 TTL 이 지나면 DART 를 다시 읽는다(10초 안팎)."""
    data, err = _api("/api/stock/disclosures", {"market": market, "code": code}, timeout=30)
    if err:
        return None, err
    since = _since(days)
    extract_dir = os.path.join(BASE_DIR, "cache", "earnings_extract")
    items = []
    for it in data.get("items") or []:
        if (it.get("date") or "") < since:
            continue
        if market == "US":
            kind = it.get("form") or "SEC"
            line = f"[{kind}] {it.get('title') or ''}"
            row = {"date": it.get("date"), "kind": kind, "line": line, "report_date": it.get("report_date"), "url": it.get("url")}
        else:
            kind = "정기공시" if it.get("periodic") else "공시"
            line = f"[{kind}] {it.get('title') or ''}"
            row = {"date": it.get("date"), "kind": kind, "line": line, "rcept_no": it.get("rcept_no"), "url": it.get("url")}
            if it.get("remark"):
                row["remark"] = it["remark"]
            ex = _read_json_file(os.path.join(extract_dir, f"{it.get('rcept_no')}.json")) if it.get("rcept_no") else None
            if ex and ex.get("reason"):   # 잠정실적 추출 저장본 (공시 요약·텔레그램 공용)
                row["earnings_extract"] = {"period": ex.get("period"), "fs_type": ex.get("fs_type"), "unit": ex.get("unit"), "reason": ex.get("reason")}
        items.append(row)
        if limit and len(items) >= limit:
            break
    return items, None


def _summary(market, code):
    return _api("/api/stock/summary", {"market": market, "code": code}, timeout=30)


@mcp.tool
def get_stock_summary(market: str, code: str) -> dict:
    """종목 요약 한 장: 이름·거래소·현재가·등락률·시가총액·PER·PBR·fPER·fPBR·목표주가 평균·ROE·결산월·연결/별도·워치리스트 여부
    + 최근 공시 3건 + (한국) 사업 개요 저장본. 금액 표기: 한국 '조/억'(원), 미국 '$…B/M'. 기준 시각은 as_of.
    market: 'kr'|'us', code: 한국 6자리 코드 또는 미국 티커."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    s, err = _summary(market, code)
    if err:
        return err
    disc, derr = _disclosure_items(market, code, 365, limit=3)
    overview = None
    if market == "KR":
        ov = _read_json_file(os.path.join(BASE_DIR, "business_overview_cache.json")) or {}
        overview = (ov.get(code) or {}).get("overview")
    cur = "USD" if market == "US" else "KRW"
    out = {
        "ok": True, "market": market.lower(), "code": code, "name": s.get("name"), "exchange": s.get("exchange"), "currency": cur,
        "price": _per_share(market, s.get("price")), "change_pct": _r(s.get("change_pct")), "prev_close": _per_share(market, s.get("prev_close")),
        "market_cap": _money(market, s.get("market_cap")), "market_cap_basis": s.get("market_cap_basis"), "shares": _shares(s.get("shares")),
        "per": _r(s.get("per"), 1), "pbr": _r(s.get("pbr")), "valuation_basis": s.get("valuation_basis"),
        "fper": _r(s.get("fper"), 1), "fpbr": _r(s.get("fpbr")), "forward_year": s.get("f_year"), "forward_eps": s.get("f_eps"), "forward_source": s.get("fsource"),
        "target_price_mean": _per_share(market, s.get("target_price_mean")), "opinion_mean": s.get("opinion_mean"), "opinion_scale": s.get("opinion_scale"),
        "analysts_n": s.get("analysts_n"),
        "roe_pct": _r(s.get("roe"), 1), "fiscal_month": s.get("fiscal_month"), "fs_div": s.get("fs_div_label"),
        "financials_ready": bool(s.get("financials_ready")), "in_watchlist": bool(s.get("in_watchlist")),
        "business_overview": overview,
        "recent_disclosures": disc if disc is not None else [], "disclosures_error": derr.get("message") if derr else None,
        "as_of": s.get("as_of"),
    }
    return out


# ---------------------------------------------------------------- ③ 재무 3표
_EPS_KEYS = {"eps_basic", "eps_diluted"}


def _fin_value(market, key, v):
    if v is None:
        return None
    if key in _EPS_KEYS:
        return round(v, 2) if market == "US" else round(v)
    return round(v / 1e6, 1) if market == "US" else round(v / 1e8, 1)


def _build_financials(market, fin, years, quarters, sheets, with_ttm=True):
    p = fin["periods"]
    annual = p["annual"][-years:] if years > 0 else []
    quarter = p["quarter"][-quarters:] if quarters > 0 else []
    ttm = p["ttm"][-quarters:] if (quarters > 0 and with_ttm) else []
    statements = {}
    for sheet in sheets:
        items = fin["statements"].get(sheet, {}).get("items", {})
        order = (fin.get("order") or {}).get(sheet) or list(items)
        rows = []
        for k in order:
            it = items.get(k)
            if not it:
                continue
            row = {"key": k, "label": it.get("label")}
            if annual:
                row["annual"] = {lb: _fin_value(market, k, (it.get("annual") or {}).get(lb)) for lb in annual}
            if quarter:
                row["quarter"] = {lb: _fin_value(market, k, (it.get("quarter") or {}).get(lb)) for lb in quarter}
            if ttm and sheet != "BS" and it.get("ttm"):
                row["ttm"] = {lb: _fin_value(market, k, it["ttm"].get(lb)) for lb in ttm}
            rows.append(row)
        statements[sheet] = rows
    m = fin.get("metrics") or {}
    metrics = {"annual": {lb: m.get("annual", {}).get(lb) for lb in annual}}
    if quarter:
        metrics["quarter"] = {lb: m.get("quarter", {}).get(lb) for lb in quarter}
    if ttm:
        metrics["ttm"] = {lb: m.get("ttm", {}).get(lb) for lb in ttm}
    return {"periods": {"annual": annual, "quarter": quarter, "ttm": ttm}, "statements": statements, "metrics": metrics}


@mcp.tool
def get_financials(market: str, code: str, years: int = 5, quarters: int = 4, statements: str = "all") -> dict:
    """재무 3표(저장본만, 외부 조회 없음). 단위: 한국 '억원', 미국 'USD M'(백만 달러); EPS 는 원/USD.
    years: 연간 기간 수(최대 10), quarters: 최근 분기 수(최대 20, 0 이면 연간만), statements: 'all'|'is'|'bs'|'cf'.
    annual 은 회계연도 말(YYYY.MM), quarter 는 3개월 값, ttm 은 4분기 누적(IS·CF). metrics 는 % (영업이익률·순이익률·ROE·부채비율·YoY).
    저장본이 없으면 error='not_cached' — 대시보드 종목종합정보에서 종목을 한 번 열면 생성된다. 응답이 40KB 를 넘으면 잘라서 돌려주고 hint 로 알린다."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    years = max(1, min(int(years or 5), 10))
    quarters = max(0, min(int(quarters or 0), 20))
    st = (statements or "all").strip().lower()
    sheets = {"all": ["IS", "BS", "CF"], "is": ["IS"], "bs": ["BS"], "cf": ["CF"]}.get(st)
    if not sheets:
        return {"ok": False, "error": "bad_statements", "message": "statements 는 all|is|bs|cf"}
    fin, err = _api("/api/stock/financials", {"market": market, "code": code, "years": "10" if years > 5 else "5", "cache_only": "1"}, timeout=10)
    if err:
        if err.get("error") == "not_cached":
            return {"ok": False, "error": "not_cached", "hint": "대시보드 종목종합정보에서 종목을 한 번 열면 생성됨", "market": market.lower(), "code": code}
        return err
    unit = "USD M (백만 달러), EPS 는 USD" if market == "US" else "억원, EPS 는 원"
    base = {"ok": True, "market": market.lower(), "code": code, "name": fin.get("name"), "unit": unit, "fiscal_month": fin.get("fiscal_month"),
            "fs_div": fin.get("fs_div_label"), "cache_updated_at": fin.get("cache_updated_at"), "notes": fin.get("notes") or [],
            "warnings": fin.get("warnings") or [], "missing": [f"{x.get('sheet')}:{x.get('label')}" for x in (fin.get("missing") or [])]}
    # 40KB 상한: ttm 제거 → 분기 축소 → 연간 축소 순으로 줄인다
    plan = [(years, quarters, True), (years, quarters, False), (years, min(quarters, 4), False), (years, 0, False),
            (min(years, 5), 0, False), (3, 0, False)]
    out = None
    for y, q, t in plan:
        body = _build_financials(market, fin, y, q, sheets, t)
        out = dict(base, **body)
        size = len(json.dumps(out, ensure_ascii=False))
        if size <= FIN_MAX_BYTES:
            if (y, q, t) != (years, quarters, True):
                out["hint"] = f"응답 상한(40KB) 때문에 years={y}, quarters={q}{'' if t else ', ttm 제외'} 로 줄였음. 더 보려면 statements 를 하나만 지정하거나 years 를 줄일 것"
            return out
    out["hint"] = "응답 상한(40KB)을 넘어 최소 범위로 줄였음. statements 를 하나만 지정할 것"
    return out


# ---------------------------------------------------------------- ④ 밸류에이션
def _band_position(cur, band):
    """현재 배수가 밴드(p10~p90) 어디쯤인지 한 줄"""
    if cur is None or not band:
        return None
    pts = [(10, band.get("p10")), (25, band.get("p25")), (50, band.get("p50")), (75, band.get("p75")), (90, band.get("p90"))]
    pts = [(p, v) for p, v in pts if v is not None]
    if not pts:
        return None
    if cur < pts[0][1]:
        return f"P{pts[0][0]} 아래 (역사적 하단)"
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if v1 <= cur < v2:
            return f"P{p1}~P{p2} 사이"
    return f"P{pts[-1][0]} 위 (역사적 상단)"


def _pct_rank(cur, series, key):
    vals = [x.get(key) for x in (series or []) if x.get(key) is not None]
    if cur is None or not vals:
        return None
    return round(100.0 * sum(1 for v in vals if v <= cur) / len(vals))


@mcp.tool
def get_valuation(market: str, code: str) -> dict:
    """밸류에이션: TTM PER·PBR(현재 시가총액 기준), fPER·fPBR(컨센서스), PER·PBR 밴드(P10~P90)와 현재 위치·역사적 백분위,
    연도별 EPS/BPS, 주식수, 기준일. 한국 밴드는 배치 저장본(valuation_bands.json), 미국은 재무 저장본으로 계산한 하루 저장본.
    밴드가 없으면 band=None 과 band_error 를 돌려준다."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    s, err = _summary(market, code)
    if err:
        return err
    band, berr = _api(f"/api/valuation/band/{code}", {"market": market} if market == "US" else None, timeout=30)
    out = {
        "ok": True, "market": market.lower(), "code": code, "name": s.get("name"),
        "price": _per_share(market, s.get("price")), "market_cap": _money(market, s.get("market_cap")), "shares": _shares(s.get("shares")),
        "per_ttm": _r(s.get("per"), 1), "pbr_ttm": _r(s.get("pbr")), "valuation_basis": s.get("valuation_basis"),
        "fper": _r(s.get("fper"), 1), "fpbr": _r(s.get("fpbr")), "forward_year": s.get("f_year"), "forward_eps": s.get("f_eps"), "forward_bps": s.get("f_bps"),
        "forward_source": s.get("fsource"), "forward_as_of": s.get("f_asof"),
        "roe_pct": _r(s.get("roe"), 1), "as_of": s.get("as_of"),
    }
    if berr or not band:
        out["band"] = None
        out["band_error"] = (berr or {}).get("message") or (berr or {}).get("error") or "밴드 없음"
        return out
    series = band.get("series") or []
    out["band"] = {
        "basis": band.get("basis"), "ttm_end": band.get("ttm_end"), "computed_at": band.get("computed_at"),
        "months_n": len(series), "series_from": series[0].get("date") if series else None, "series_to": series[-1].get("date") if series else None,
        "per_band": band.get("per_band"), "pbr_band": band.get("pbr_band"),
        "per_position": _band_position(s.get("per"), band.get("per_band")), "per_percentile": _pct_rank(s.get("per"), series, "per"),
        "pbr_position": _band_position(s.get("pbr"), band.get("pbr_band")), "pbr_percentile": _pct_rank(s.get("pbr"), series, "pbr"),
        "eps_by_year": band.get("eps_by_year"), "bps_by_year": band.get("bps_by_year"),
        "latest_eps": band.get("latest_eps"), "latest_bps": band.get("latest_bps"), "latest_eps_year": band.get("latest_eps_year"),
        "eps_unit": "USD" if market == "US" else "원",
        "approx_share_months": band.get("approx_share_months"),
    }
    return out


# ---------------------------------------------------------------- ⑤ 공시
@mcp.tool
def get_disclosures(market: str, code: str, days: int = 90) -> dict:
    """최근 공시 목록(최대 365일). 한국: DART 전체 공시(임원·주요주주 거래 제외), 정기공시 표시, 잠정실적 공시는 추출 요약(earnings_extract) 첨부.
    미국: SEC 10-K·10-Q·8-K·DEF 14A 목록 + 8-K 실적 보도자료 저장본의 AI 요약(있을 때). AI 호출·새 요약 생성은 하지 않는다.
    각 항목 line 은 '[종류] 제목' 한 줄."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    days = max(1, min(int(days or 90), 365))
    items, err = _disclosure_items(market, code, days)
    if err:
        return err
    out = {"ok": True, "market": market.lower(), "code": code, "days": days, "since": _since(days), "count": len(items), "items": items}
    if market == "US":
        k, kerr = _api("/api/stock/sec8k", {"code": code}, timeout=10)
        if not kerr and k:
            since = _since(days)
            pr = []
            for it in k.get("items") or []:
                if (it.get("date") or "") < since:
                    continue
                sm = it.get("summary") or {}
                pr.append({"date": it.get("date"), "exhibit": it.get("exhibit"), "filename": it.get("filename"),
                           "line": f"[8-K 실적] {sm.get('분기') or ''} " + _trim((sm.get("실적핵심") or [""])[0], 160) if sm else f"[8-K 실적] {it.get('filename')} (요약 없음)",
                           "summary": sm or None, "drive": it.get("drive")})
            out["earnings_press_releases"] = pr
    return out


# ---------------------------------------------------------------- ⑥ 리포트
@mcp.tool
def get_reports(market: str, code: str, months: int = 3, limit: int = 20) -> dict:
    """증권사 리포트 저장본(텔레그램 리포트 DB, 최대 3개월): 날짜·증권사·제목·투자의견·목표가(이전 목표가·변경)·AI 요약·추정실적,
    집계(목표가 평균/최소/최대·상승여력·의견 분포·컨센서스 추정). 새 검색·다운로드·AI 정리는 시작하지 않는다.
    목표가·추정실적 단위: 한국 원·억원, 미국 USD·USD M. 원문 PDF 는 Drive 경로 문자열(drive)만."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    months = max(1, min(int(months or 3), 3))
    limit = max(1, min(int(limit or 20), 100))
    rp, err = _api("/api/stock/reports", {"market": market, "code": code, "start": "0", "ai_limit": "0"}, timeout=30)
    if err:
        return err
    since = _since(30 * months)
    items = []
    for it in rp.get("items") or []:
        if (it.get("date") or "")[:10] < since:
            continue
        items.append({
            "date": (it.get("date") or "")[:10], "broker": it.get("broker"), "analyst": it.get("analyst"), "title": it.get("title"), "type": it.get("type"),
            "opinion": it.get("opinion"), "target": it.get("target"), "prev_target": it.get("prev_target"), "target_change": it.get("target_change"),
            "summary": it.get("summary") or [], "estimates": it.get("estimates") or [], "flags": it.get("flags") or [],
            "ai_status": it.get("ai_status"), "drive": it.get("drive"),
        })
        if len(items) >= limit:
            break
    agg = rp.get("aggregate") or {}
    cur = "USD" if market == "US" else "KRW"
    return {
        "ok": True, "market": market.lower(), "code": code, "name": rp.get("name"), "currency": cur,
        "period": {"from": since, "to": date.today().isoformat()}, "last_search": rp.get("last_search"),
        "total_in_db": rp.get("count"), "count": len(items), "items": items,
        "aggregate": {k: agg.get(k) for k in ("brokers_n", "reports_ok", "reports_total", "target_mean", "target_min", "target_max",
                                              "target_upside_pct", "opinion_dist", "estimates")},
        "drive_dir": (rp.get("drive") or {}).get("dir"),
    }


# ---------------------------------------------------------------- ⑦⑧ 컨콜
def _calls_us(code):
    return _api("/api/stock/calls", {"market": "US", "code": code, "start": "0"}, timeout=30)


def _quarter_rows(calls):
    return [{"quarter": q.get("quarter"), "period_end": q.get("label"), "filed": q.get("filed"), "transcript": q.get("transcript"),
             "summary": q.get("summary_status"), "translation_ko": bool(q.get("translation"))} for q in calls.get("quarters") or []]


@mcp.tool
def get_call_summary(market: str, code: str, quarter: str | None = None) -> dict:
    """실적 컨콜 요약 저장본. 미국: 분기(예 '2027Q2', 회계연도 기준)별 AI 요약(실적핵심·가이던스·전망·리스크·질의응답·톤)과 사용 가능한 분기 목록.
    quarter 를 비우면 요약이 있는 최근 분기 1건. 한국: 실적발표·컨콜 관련 증권사 리포트·텔레그램 메모 목록(저장본).
    새 녹취록 수집·요약 생성은 하지 않는다 (없으면 status 로 알림)."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    if market == "KR":
        d, err = _api("/api/stock/calls", {"market": "KR", "code": code}, timeout=30)
        if err:
            return err
        items = [{"source": it.get("source"), "date": it.get("date"), "broker": it.get("broker") or it.get("channel"), "title": _trim(it.get("title"), 200),
                  "summary": it.get("summary") or [], "opinion": it.get("opinion"), "target": it.get("target"), "ai_status": it.get("ai_status")}
                 for it in d.get("items") or []]
        return {"ok": True, "market": "kr", "code": code, "name": d.get("name"), "note": "한국 종목은 컨콜 녹취록 대신 실적발표·컨콜 관련 리포트·메모 목록",
                "count": len(items), "items": items[:30]}
    calls, err = _calls_us(code)
    if err:
        return err
    rows = _quarter_rows(calls)
    qs = calls.get("quarters") or []
    if not qs:
        return {"ok": False, "error": "not_cached", "message": "재무 저장본이 없어 분기 목록을 만들 수 없음", "hint": "대시보드에서 종목을 한 번 열 것"}
    if quarter:
        pick = next((q for q in qs if q.get("quarter") == quarter.strip().upper()), None)
        if not pick:
            return {"ok": False, "error": "bad_quarter", "message": f"분기 {quarter} 없음", "available": rows}
    else:
        pick = next((q for q in qs if q.get("summary")), None)
        if not pick:
            return {"ok": False, "error": "not_cached", "message": "요약된 분기가 없음", "hint": "대시보드 컨콜 섹션에서 요약을 만든 뒤 조회", "available": rows}
    out = {"ok": True, "market": "us", "code": code, "name": calls.get("name"), "quarter": pick.get("quarter"), "period_end": pick.get("label"),
           "filed": pick.get("filed"), "transcript": pick.get("transcript"), "summary_status": pick.get("summary_status"),
           "summary": pick.get("summary"), "available": rows}
    if not pick.get("summary"):
        out["hint"] = "이 분기는 요약이 없음 (녹취록 " + ("있음" if pick.get("transcript") == "ok" else "없음") + "). 대시보드에서 요약 생성 필요"
    return out


@mcp.tool
def get_call_comparison(market: str, code: str) -> dict:
    """직전 분기 vs 이번 분기 컨콜 비교 저장본(미국만). 한줄결론·실적·가이던스이행·가이던스변화·경영진view·리스크변화·섹터전망·자본배분·
    직전언급추적·질문초점변화 + 키워드 빈도(직전/이번/증감). 최근 분기부터 저장된 비교를 찾아 1건 돌려주며, 없으면 error='not_cached' (생성하지 않음)."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    if market != "US":
        return {"ok": False, "error": "not_supported", "message": "컨콜 비교는 미국 종목만"}
    calls, err = _calls_us(code)
    if err:
        return err
    qs = [q for q in calls.get("quarters") or [] if q.get("transcript") == "ok"]
    checked = []
    for q in qs[:4]:
        d, derr = _api("/api/stock/calls/compare", {"code": code, "quarter": q["quarter"]}, timeout=10)
        if derr:
            checked.append({"quarter": q["quarter"], "status": derr.get("error")})
            continue
        if d.get("status") == "ok":
            return {"ok": True, "market": "us", "code": code, "name": calls.get("name"), "quarter": d.get("quarter"), "prev_quarter": d.get("prev_quarter"),
                    "generated_at": d.get("at"), "model": d.get("model"), "compare": d.get("compare"), "keyword_freq": d.get("키워드빈도")}
        checked.append({"quarter": q["quarter"], "status": d.get("status"), "ready": d.get("ready"), "missing": d.get("missing")})
    return {"ok": False, "error": "not_cached", "message": "저장된 컨콜 비교 없음", "hint": "대시보드 컨콜 섹션에서 '직전 분기와 비교' 를 만든 뒤 조회",
            "checked": checked}


# ---------------------------------------------------------------- ⑨ 나의 판단
def _note_public(n):
    n = n or {}
    return {"name": n.get("name"), "version": n.get("version"), "created_at": n.get("created_at"), "updated_at": n.get("updated_at"),
            "fields": {k: (n.get("fields") or {}).get(k) for k in NOTE_FIELDS}, "field_labels": NOTE_FIELDS,
            "memos": [{"id": m.get("id"), "at": m.get("at"), "text": m.get("text")} for m in n.get("memos") or []],
            "history": [{"at": h.get("at"), "field": h.get("field"), "label": h.get("label"), "before": _trim(h.get("before"), 300), "after": _trim(h.get("after"), 300)}
                        for h in (n.get("history") or [])[:30]],
            "has_content": bool(n.get("has_content"))}


@mcp.tool
def get_notes(market: str, code: str) -> dict:
    """나의 판단 읽기: 4칸(idea=투자아이디어, assumptions=핵심가정, followup=Follow-up 포인트, risks=Risk 포인트) + 날짜 메모(memos) + 변경 이력(history, 최신순 30건)."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    d, err = _api("/api/stock/notes", {"market": market, "code": code}, timeout=10)
    if err:
        return err
    return {"ok": True, "market": market.lower(), "code": code, "note": _note_public(d.get("note"))}


@mcp.tool
def save_note(market: str, code: str, field: str, text: str, date_memo: str = "") -> dict:
    """나의 판단 저장(유일한 쓰기 도구). field 는 idea|assumptions|followup|risks (한글 이름도 허용) 중 하나, text 로 그 칸을 통째로 바꾼다
    (기존 내용은 변경 이력에 남고 Drive 마크다운에도 반영). date_memo 를 주면 날짜 메모 1건을 추가로 남긴다. 저장 후 전체 노트를 돌려준다.
    칸을 비우려면 text='' 를 보낼 것."""
    require_allowed_email()
    market, code, err = _target(market, code)
    if err:
        return err
    f = (field or "").strip()
    key = f if f in NOTE_FIELDS else NOTE_FIELD_BY_LABEL.get(f) or NOTE_FIELD_BY_LABEL.get(f.lower())
    if key not in NOTE_FIELDS:
        return {"ok": False, "error": "bad_field", "message": "field 는 idea|assumptions|followup|risks 중 하나", "field_labels": NOTE_FIELDS}
    cur, err = _api("/api/stock/notes", {"market": market, "code": code}, timeout=10)
    if err:
        return err
    version = (cur.get("note") or {}).get("version", 0)
    body = {"market": market, "code": code, "fields": {key: text if text is not None else ""}, "version": version}
    d, err = _api("/api/stock/notes", timeout=30, method="PUT", body=body)
    if err:
        if err.get("error") == "conflict":
            return {"ok": False, "error": "conflict", "message": "다른 곳에서 먼저 수정됨. 다시 시도할 것"}
        return err
    out = {"ok": True, "market": market.lower(), "code": code, "field": key, "label": NOTE_FIELDS[key], "changed": d.get("changed") or [],
           "export": d.get("export")}
    note = d.get("note")
    if (date_memo or "").strip():
        m, merr = _api("/api/stock/notes/memo", timeout=30, method="POST", body={"market": market, "code": code, "text": date_memo.strip(), "source": "대시보드"})
        if merr:
            out["memo_error"] = merr.get("message") or merr.get("error")
        else:
            out["memo"] = m.get("memo")
            note = m.get("note") or note
    out["note"] = _note_public(note)
    log.info("save_note %s %s field=%s changed=%s", market, code, key, out["changed"])
    return out


# ====================================================================== 운영 진단 (읽기 전용)
import subprocess  # noqa: E402

_HOME = os.path.expanduser("~")
_CACHE = os.path.join(BASE_DIR, "cache")
_ULOGS = os.path.join(_HOME, "Library", "Logs")
RESP_MAX = 60_000          # json.dumps 길이 상한
TAIL_MAX_BYTES = 2_000_000  # read_log 가 파일 끝에서 읽는 최대 바이트
LLM_MAX_BYTES = 20_000_000  # llm_stats 가 읽는 최대 바이트

LOG_FILES = {
    "app": os.path.join(BASE_DIR, "app.log"),
    "live_scheduler": os.path.join(_CACHE, "live_scheduler.log"),
    "mcp": os.path.join(LOG_DIR, "mcp_server.log"),
    "mcp_launchd": os.path.join(LOG_DIR, "mcp_server.launchd.log"),
    "watchdog": os.path.join(LOG_DIR, "watchdog.log"),
    "watchdog_launchd": os.path.join(LOG_DIR, "watchdog_launchd.log"),
    "nightly_restart": os.path.join(BASE_DIR, "nightly_restart.log"),
    "socket_monitor": os.path.join(BASE_DIR, "socket_monitor.log"),
    "refinery": os.path.join(_ULOGS, "refinery-cache.log"),
    "valuation_band": os.path.join(BASE_DIR, "valuation_band.log"),
    "valuation_band_err": os.path.join(BASE_DIR, "valuation_band.err"),
    "valuation_screener": os.path.join(BASE_DIR, "valuation_screener.log"),
    "valuation_screener_err": os.path.join(BASE_DIR, "valuation_screener.err"),
    "stock_leaders_kr": os.path.join(BASE_DIR, "stock_leaders_kr.log"),
    "sector_leaders_kr": os.path.join(BASE_DIR, "sector_leaders_kr.log"),
    "sector_leaders_us": os.path.join(BASE_DIR, "sector_leaders_us.log"),
    "consensus_snapshot": os.path.join(BASE_DIR, "consensus_snapshot.log"),
    "quarterly_eps": os.path.join(LOG_DIR, "quarterly_eps.log"),
    "indicators_tracker": os.path.join(_CACHE, "indicators_tracker.log"),
    "korea_export": os.path.join(BASE_DIR, "korea_export_update.log"),
}
for _m in ("us", "kr", "jp", "eu"):
    LOG_FILES[f"batch_{_m}"] = os.path.join(_CACHE, f"batch_top_gainers_{_m}.log")
    LOG_FILES[f"batch_launchd_{_m}"] = f"/tmp/dashboard_batch_{_m}.log"
    LOG_FILES[f"batch_launchd_err_{_m}"] = f"/tmp/dashboard_batch_{_m}.err"

CACHE_GROUPS = {
    "market": ["cache/top_gainers_us.json", "cache/top_gainers_kr.json", "cache/top_gainers_jp.json", "cache/top_gainers_eu.json",
               "cache/market_map_us.json", "cache/market_map_jp.json", "cache/live_scheduler.log"],
    "stock": ["cache/kr_financials/", "cache/us_financials/", "cache/us_disclosures/", "cache/concept_maps/", "cache/reports/", "cache/calls/",
              "cache/peers/", "cache/earnings_extract/", "cache/forward_estimates/", "cache/us_bands/", "cache/valuation_bands.json", "stock_notes/"],
    "daily": ["cache/refinery.json", "cache/sector_leaders.json", "cache/stock_leaders.json", "cache/indicators_history.json", "cache/disparity.json",
              "cache/consensus_snapshots/", "cache/analyst_actions/", "cache/briefs/", "cache/yahoo_backoff.json", "cache/tg_inbox.json"],
    "llm": ["cache/_ai_state.json", "logs/llm_calls.jsonl"],
}
_STAMP_KEYS = ("as_of", "data_as_of", "updated_at", "cache_updated_at", "date", "computed_at")
_NO_PARSE = {"cache/tg_inbox.json"}  # 크기·mtime 만
_STALE_HOURS = 26  # app.py top_gainers 자가치유 기준과 동일

_MASK_RES = [
    (re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}"), "***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "***"),
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"), "***"),
    (re.compile(r"(?i)\b(key|token|secret|api_key|crtfc_key)=[^&\s\"']+"), r"\1=***"),
]


def _mask(s):
    """로그·명령 출력에서 비밀값 모양(봇 토큰, sk-키, 긴 16진수, key=값)을 *** 로 가린다."""
    if not s:
        return s or ""
    for rx, rep in _MASK_RES:
        s = rx.sub(rep, s)
    return s


def _file_info(path):
    try:
        st = os.stat(path)
    except OSError:
        return {"exists": False}
    mtime = datetime.fromtimestamp(st.st_mtime, KST)
    return {"exists": True, "size": st.st_size, "mtime": mtime.isoformat(timespec="seconds"),
            "age_hours": round((datetime.now(KST) - mtime).total_seconds() / 3600, 1)}


def _dir_info(path):
    if not os.path.isdir(path):
        return {"exists": False}
    n = 0
    newest = 0.0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    out = {"exists": True, "files": n, "total_mb": round(total / 1_048_576, 1)}
    if newest:
        mt = datetime.fromtimestamp(newest, KST)
        out["newest_mtime"] = mt.isoformat(timespec="seconds")
        out["newest_age_hours"] = round((datetime.now(KST) - mt).total_seconds() / 3600, 1)
    else:
        out["newest_mtime"] = None
        out["newest_age_hours"] = None
    return out


def _tail_lines(path, max_bytes):
    """파일 끝에서 max_bytes 만 읽어 줄 목록으로. (잘린 첫 줄은 버림)"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]
    if lines and lines[-1] == "":
        lines.pop()
    return lines, start > 0


def _fit(payload, shrink):
    """응답이 RESP_MAX 를 넘으면 shrink(payload) 로 줄여 가며 맞춘다. shrink 는 더 줄일 수 없으면 False."""
    hint = None
    for _ in range(12):
        if len(json.dumps(payload, ensure_ascii=False)) <= RESP_MAX:
            break
        if not shrink(payload):
            break
        hint = f"응답 {RESP_MAX // 1000}KB 상한을 넘어 줄을 절반씩 줄였음. lines 를 줄이거나 grep 을 쓸 것"
    if hint:
        payload["hint"] = hint
    return payload


@mcp.tool
def read_log(name: str, lines: int = 100, grep: str = "") -> dict:
    """운영 로그 마지막 N줄 읽기(읽기 전용). name 은 allowlist 이름(name='list' 로 전체 목록·존재·크기 확인), lines 1~500,
    grep 은 대소문자 무시 부분 문자열 필터. 경로는 받지 않으며 비밀값 모양은 *** 로 가려진다. 배치·스케줄러·MCP·watchdog 오류 확인용."""
    require_allowed_email()
    key = (name or "").strip()
    if key == "list":
        return {"ok": True, "server_time": datetime.now(KST).isoformat(timespec="seconds"), "count": len(LOG_FILES),
                "logs": {k: {"path": p, **_file_info(p)} for k, p in sorted(LOG_FILES.items())}}
    path = LOG_FILES.get(key)
    if not path:
        return {"ok": False, "error": "bad_name", "message": f"모르는 로그 이름: {key}", "available": sorted(LOG_FILES)}
    info = _file_info(path)
    if not info["exists"]:
        return {"ok": False, "error": "not_found", "name": key, "path": path, "file": info, "message": "로그 파일 없음"}
    try:
        n = max(1, min(500, int(lines)))
    except (TypeError, ValueError):
        n = 100
    try:
        all_lines, truncated = _tail_lines(path, TAIL_MAX_BYTES)
    except OSError as e:
        return {"ok": False, "error": "read_error", "name": key, "path": path, "file": info, "message": type(e).__name__}
    g = (grep or "").strip()
    if g:
        gl = g.lower()
        picked = [ln for ln in all_lines if gl in ln.lower()]
    else:
        picked = all_lines
    out = {"ok": True, "name": key, "path": path, "file": info, "lines_requested": n, "lines_total_scanned": len(all_lines),
           "scanned_tail_only": truncated, "grep": g or None}

    def _render(p, k):
        p["lines_returned"] = k
        p["text"] = _mask("\n".join(picked[-k:])) if k else ""

    def _shrink(p):
        cur = p["lines_returned"]
        if cur <= 1:
            return False
        _render(p, max(1, cur // 2))
        return True

    _render(out, min(n, len(picked)))
    return _fit(out, _shrink)


def _stamps(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return {k: d[k] for k in _STAMP_KEYS if k in d and isinstance(d[k], (str, int, float))} or None


@mcp.tool
def cache_status(group: str = "all") -> dict:
    """캐시·산출물 신선도 점검(읽기 전용). group: market(시장지도·등락 상위, stale 표시) | stock(종목별 캐시 디렉터리) |
    daily(일간 배치 산출물) | llm(AI 예산 상태·호출 로그) | all. 파일은 크기·mtime·경과시간·내부 as_of 류 스탬프, 디렉터리는 파일 수·최신 mtime·용량."""
    require_allowed_email()
    g = (group or "all").strip().lower()
    if g != "all" and g not in CACHE_GROUPS:
        return {"ok": False, "error": "bad_group", "message": f"모르는 group: {g}", "available": ["all"] + list(CACHE_GROUPS)}
    rels = [r for k, lst in CACHE_GROUPS.items() if g in ("all", k) for r in lst]
    items = {}
    for rel in rels:
        path = os.path.join(BASE_DIR, rel.rstrip("/"))
        if rel.endswith("/"):
            items[rel] = _dir_info(path)
            continue
        info = _file_info(path)
        if info["exists"] and rel.endswith(".json") and rel not in _NO_PARSE and info["size"] <= 5_000_000:
            st = _stamps(path)
            if st:
                info["stamps"] = st
        if rel.startswith("cache/top_gainers_"):
            info["stale"] = (not info["exists"]) or info["age_hours"] > _STALE_HOURS
        items[rel] = info
    return {"ok": True, "group": g, "server_time": datetime.now(KST).isoformat(timespec="seconds"), "count": len(items), "items": items}


def _p90(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


@mcp.tool
def llm_stats(days: int = 1, task: str = "") -> dict:
    """llm_client 호출 로그(logs/llm_calls.jsonl) 집계(읽기 전용). 최근 days일(1~30) 동안 총/성공/실패/대체(fallback) 건수,
    backend·model·task 별 건수와 평균·p90 소요시간, 최근 실패·대체 20건. task 를 주면 그 작업만."""
    require_allowed_email()
    path = os.path.join(LOG_DIR, "llm_calls.jsonl")
    info = _file_info(path)
    if not info["exists"]:
        return {"ok": False, "error": "not_found", "path": path, "file": info}
    try:
        d = max(1, min(30, int(days)))
    except (TypeError, ValueError):
        d = 1
    since = datetime.now(KST) - timedelta(days=d)
    t_filter = (task or "").strip() or None
    try:
        lines, _trunc = _tail_lines(path, LLM_MAX_BYTES)
    except OSError as e:
        return {"ok": False, "error": "read_error", "path": path, "message": type(e).__name__}
    rows = []
    scanned = 0
    for ln in lines:
        if not ln.strip():
            continue
        scanned += 1
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        ts = r.get("ts")
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=KST)
        except (TypeError, ValueError):
            continue
        if t < since:
            continue
        if t_filter and r.get("task") != t_filter:
            continue
        rows.append(r)
    by_backend, by_model, by_task = {}, {}, {}
    ok_n = fail_n = fb_n = 0
    durs = {}
    for r in rows:
        ok = bool(r.get("ok"))
        fb = bool(r.get("fallback"))
        ok_n += ok
        fail_n += not ok
        fb_n += fb
        b = r.get("backend") or "?"
        m = r.get("model") or "?"
        tk = r.get("task") or "?"
        by_backend[b] = by_backend.get(b, 0) + 1
        by_model[m] = by_model.get(m, 0) + 1
        bt = by_task.setdefault(tk, {"n": 0, "failed": 0, "fallback": 0, "avg_ms": None, "p90_ms": None, "output_tokens_sum": 0})
        bt["n"] += 1
        bt["failed"] += not ok
        bt["fallback"] += fb
        bt["output_tokens_sum"] += int(r.get("output_tokens") or 0)
        if isinstance(r.get("duration_ms"), (int, float)):
            durs.setdefault(tk, []).append(r["duration_ms"])
    for tk, ds in durs.items():
        by_task[tk]["avg_ms"] = int(sum(ds) / len(ds))
        by_task[tk]["p90_ms"] = int(_p90(ds))
    failures = [{"ts": r.get("ts"), "task": r.get("task"), "backend": r.get("backend"), "error": _mask(str(r.get("error") or ""))[:150]}
                for r in rows if not r.get("ok")][-20:]
    fallbacks = [{"ts": r.get("ts"), "task": r.get("task"), "primary_backend": (r.get("extra") or {}).get("primary_backend") if isinstance(r.get("extra"), dict) else None,
                  "backend": r.get("backend"), "fallback_reason": _mask(str(r.get("fallback_reason") or ""))[:150]}
                 for r in rows if r.get("fallback")][-20:]
    return {"ok": True, "days": d, "task": t_filter, "server_time": datetime.now(KST).isoformat(timespec="seconds"), "file": info,
            "total": len(rows), "ok_n": ok_n, "failed": fail_n, "fallback_n": fb_n, "by_backend": by_backend, "by_model": by_model, "by_task": by_task,
            "recent_failures": failures, "recent_fallbacks": fallbacks,
            "first_ts": rows[0].get("ts") if rows else None, "last_ts": rows[-1].get("ts") if rows else None, "rows_scanned": scanned}


@mcp.tool
def ai_budget_status() -> dict:
    """AI 호출 일일 예산(ai_budget) 현황(읽기 전용): 오늘 날짜, kind 별 사용량·한도·잔여. 한도를 넘어 AI 요약이 멈췄는지 확인할 때."""
    require_allowed_email()
    try:
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        import ai_budget  # noqa: E402
        snap = ai_budget.snapshot()
    except Exception as e:
        return {"ok": False, "error": "import_failed", "message": f"{type(e).__name__}: {_mask(str(e))[:200]}"}
    counts = snap.get("counts") or {}
    limits = snap.get("limits") or {}
    remaining = {}
    for kind in sorted(set(counts) | set(limits)):
        lim = ai_budget.resolve_limit(kind)
        used = int(counts.get(kind) or 0)
        remaining[kind] = {"used": used, "limit": lim, "remaining": (None if lim is None else max(0, lim - used))}
    return {"ok": True, "server_time": datetime.now(KST).isoformat(timespec="seconds"), **snap, "remaining": remaining}


def _git(*args, timeout=10):
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/usr/local/bin:" + env.get("PATH", "")
    try:
        p = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}"
    if p.returncode != 0:
        return None, _mask((p.stderr or p.stdout).strip())[:300] or f"exit {p.returncode}"
    return p.stdout.rstrip("\n"), None


@mcp.tool
def git_status() -> dict:
    """저장소 상태(읽기 전용): 현재 브랜치, HEAD, 최근 커밋 8건, 변경 파일 수·경로(최대 40), upstream 대비 ahead/behind. diff 내용은 돌려주지 않는다."""
    require_allowed_email()
    out = {"ok": True, "server_time": datetime.now(KST).isoformat(timespec="seconds"), "cwd": BASE_DIR}
    v, e = _git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = v if e is None else {"error": e}
    v, e = _git("log", "-1", "--format=%h %ci %s")
    out["head"] = _mask(v) if e is None else {"error": e}
    v, e = _git("log", "-8", "--format=%h|%ci|%s")
    if e is None:
        rec = []
        for ln in v.splitlines():
            parts = ln.split("|", 2)
            if len(parts) == 3:
                rec.append({"hash": parts[0], "date": parts[1], "subject": _mask(parts[2])})
        out["recent"] = rec
    else:
        out["recent"] = {"error": e}
    v, e = _git("status", "--porcelain")
    if e is None:
        lines = [ln for ln in v.splitlines() if ln.strip()]
        out["dirty"] = len(lines)
        out["dirty_files"] = [_mask(ln[3:].strip()) for ln in lines[:40]]
    else:
        out["dirty"] = {"error": e}
        out["dirty_files"] = []
    v, e = _git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if e is None and v:
        parts = v.split()
        out["ahead_behind"] = {"ahead": int(parts[0]), "behind": int(parts[1])} if len(parts) == 2 else {"error": "parse"}
    else:
        el = (e or "").lower()
        out["ahead_behind"] = None if ("upstream" in el or "@{u}" in el) else {"error": e}
    if any(isinstance(out.get(k), dict) and "error" in out[k] for k in ("branch", "head", "recent", "dirty")):
        out["ok"] = False
    return out


@mcp.tool
def launchd_status() -> dict:
    """launchd 에이전트(com.yun.*) 상태(읽기 전용): label·pid(None 이면 대기/미실행)·마지막 종료코드, launchd/ 폴더 plist 와 대조해
    로드되지 않은 label 을 not_loaded 로. 배치가 안 돌 때 먼저 볼 것."""
    require_allowed_email()
    env = dict(os.environ)
    env["PATH"] = "/bin:/usr/bin:/usr/local/bin:" + env.get("PATH", "")
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": type(e).__name__}
    if p.returncode != 0:
        return {"ok": False, "error": "launchctl_failed", "stderr": _mask(p.stderr or "")[:300]}
    agents = []
    loaded = set()
    for ln in p.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3 or not parts[2].startswith("com.yun."):
            continue
        pid = None if parts[0] in ("-", "") else int(parts[0]) if parts[0].isdigit() else parts[0]
        try:
            status = int(parts[1])
        except ValueError:
            status = parts[1]
        agents.append({"label": parts[2], "pid": pid, "last_exit_status": status})
        loaded.add(parts[2])
    agents.sort(key=lambda a: a["label"])
    plist_dir = os.path.join(BASE_DIR, "launchd")
    plist_labels = sorted(f[:-6] for f in os.listdir(plist_dir) if f.endswith(".plist")) if os.path.isdir(plist_dir) else []
    not_loaded = [lb for lb in plist_labels if lb not in loaded]
    return {"ok": True, "server_time": datetime.now(KST).isoformat(timespec="seconds"), "running": sum(1 for a in agents if isinstance(a["pid"], int)),
            "count": len(agents), "agents": agents, "plist_count": len(plist_labels), "not_loaded": not_loaded,
            "nonzero_exit": [a["label"] for a in agents if a["last_exit_status"] not in (0, None)]}


if __name__ == "__main__":
    log.info(
        "MCP 서버 시작: fastmcp=%s base_url=%s callback=%s%s mcp=%s%s",
        fastmcp.__version__,
        BASE_URL,
        BASE_URL,
        auth._redirect_path,
        BASE_URL,
        MCP_PATH,
    )
    try:
        import asyncio
        _tools = asyncio.run(mcp.list_tools(run_middleware=False))
        log.info("등록 도구 %d개: %s", len(_tools), ", ".join(sorted(t.name for t in _tools)))
    except Exception as e:  # 목록 출력 실패가 서버 시작을 막지 않도록
        log.warning("도구 목록 출력 실패: %s", type(e).__name__)
    mcp.run(transport="http", host="127.0.0.1", port=8100, path=MCP_PATH, show_banner=False)
