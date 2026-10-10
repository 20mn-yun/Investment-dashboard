"""한국 종목 재무 3표 (손익계산서·재무상태표·현금흐름표) — DART fnlttSinglAcntAll.

처음 조회할 때 받아 cache/kr_financials/<종목코드>.json.gz 에 저장하고(자본변동표 행 제외, gzip),
이후에는 저장본에서 계산한다. 조회 API 는 기다리지 않는다(get_financials_nowait: 뒤에서 받아오며 진행률 반환).
  - 보고서 종류별(1분기·반기·3분기·사업) 호출, 연결(CFS) 먼저 → 자료 없으면 별도(OFS)
  - 정상 응답은 계속 보관. "자료 없음"(013)은 공시 기한이 지난 기간이면 보관, 최근 기간이면 하루 뒤 다시 확인
  - 호출 한도 초과·점검·통신 오류는 저장하지 않는다
기간 규칙 (DART bsns_year):
  - 결산 12월: bsns_year=Y 의 1분기·반기·3분기·사업보고서 → 회계연도 Y.12
  - 결산 M월(≠12): bsns_year=Y 사업보고서 → 회계연도 Y.M, bsns_year=Y 의 1분기·반기·3분기 → 회계연도 (Y+1).M
"""
import calendar
import gzip
import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime

import requests

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "kr_financials")
DART_URL = "https://opendart.fss.or.kr/api/"

DEFAULT_YEARS = 7          # 기본 수집 범위 (끝난 회계연도 수, 진행 중인 연도는 별도로 포함)
MAX_YEARS = 10
MIN_INTERVAL = 0.25        # DART 호출 간격 (초, 0.2 이상)
MAX_RETRIES = 3            # 실패 시 재시도 (간격 0.5 → 1 → 2초)
ACC_MT_TTL_DAYS = 30       # 결산월 재확인 주기
FILING_GRACE_DAYS = 15     # 법정 공시 기한 뒤 여유 (지연·정정 공시)

# 보고서 종류: (키, reprt_code, 회계연도 말에서 몇 달 전에 끝나는지, 법정 공시 기한 일수)
REPORTS = [
    ("Q1", "11013", 9, 45),
    ("H1", "11012", 6, 45),
    ("Q3", "11014", 3, 45),
    ("FY", "11011", 0, 90),
]
# 저장할 필드 (자본변동표(SCE)를 뺀 모든 행을 남기되 이 필드만)
ROW_FIELDS = ["sj_div", "account_id", "account_nm", "account_detail", "thstrm_amount", "thstrm_add_amount", "ord"]

# ===== 항목 대응 규칙 (한 곳에 모음) =====
# (표, 키, 이름, 성격, account_id 후보, account_nm 후보[공백·괄호 제거 후 비교])
#   표: IS(손익: IS 없으면 CIS 사용) / BS / CF
#   성격: flow(기간 값) / stock(시점 값) / eps(주당이익, 기간 값 근사)
RULES = [
    ("IS", "revenue", "매출액", "flow", ["ifrs-full_Revenue"], ["매출액", "매출", "영업수익", "수익"]),
    ("IS", "cogs", "매출원가", "flow", ["ifrs-full_CostOfSales"], ["매출원가"]),
    ("IS", "gross_profit", "매출총이익", "flow", ["ifrs-full_GrossProfit"], ["매출총이익"]),
    ("IS", "sga", "판매관리비", "flow",
     ["dart_TotalSellingGeneralAdministrativeExpenses", "ifrs-full_SellingGeneralAndAdministrativeExpense"],
     ["판매비와관리비", "판매관리비"]),
    ("IS", "operating_income", "영업이익", "flow",
     ["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"], ["영업이익", "영업손익"]),
    ("IS", "other_income", "기타수익", "flow", ["dart_OtherGains", "ifrs-full_OtherIncome"], ["기타수익", "기타영업외수익"]),
    ("IS", "other_expense", "기타비용", "flow", ["dart_OtherLosses", "ifrs-full_OtherExpense"], ["기타비용", "기타영업외비용"]),
    ("IS", "finance_income", "금융수익", "flow", ["ifrs-full_FinanceIncome"], ["금융수익"]),
    ("IS", "finance_cost", "금융비용", "flow", ["ifrs-full_FinanceCosts"], ["금융비용", "금융원가"]),
    ("IS", "equity_method", "지분법손익", "flow",
     ["ifrs-full_ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],
     ["지분법이익", "지분법손익", "지분법평가손익", "관계기업및공동기업투자손익", "관계기업투자손익"]),
    ("IS", "pretax_income", "법인세차감전순이익", "flow", ["ifrs-full_ProfitLossBeforeTax"],
     ["법인세비용차감전순이익", "법인세차감전순이익", "법인세비용차감전계속사업이익"]),
    ("IS", "income_tax", "법인세", "flow",
     ["ifrs-full_IncomeTaxExpenseContinuingOperations", "ifrs-full_IncomeTaxExpense"], ["법인세비용", "법인세"]),
    ("IS", "net_income", "당기순이익", "flow", ["ifrs-full_ProfitLoss"], ["당기순이익", "당기순손익"]),
    ("IS", "net_income_owner", "지배주주순이익", "flow", ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
     ["지배기업소유주지분", "지배기업의소유주에게귀속되는당기순이익", "지배기업소유주지분순이익", "지배기업의소유주지분",
      "지배회사지분순이익", "지배기업지분순이익", "지배주주지분순이익"]),
    ("IS", "net_income_minority", "비지배주주순이익", "flow",
     ["ifrs-full_ProfitLossAttributableToNoncontrollingInterests"],
     ["비지배지분", "비지배지분순이익", "비지배지분에게귀속되는당기순이익"]),
    ("IS", "eps_basic", "기본 주당이익", "eps", ["ifrs-full_BasicEarningsLossPerShare"],
     ["기본주당이익", "기본주당순이익", "기본주당순손익", "기본및희석주당이익", "기본및희석주당순이익",
      "보통주기본주당이익", "보통주기본주당순이익", "보통주기본주당순손익", "보통주기본및희석주당이익"]),
    ("IS", "eps_diluted", "희석 주당이익", "eps",
     ["ifrs-full_DilutedEarningsLossPerShare", "ifrs-full_DilutedEarningsLossPerShareFromContinuingOperations",
      "ifrs_DilutedEarningsLossPerShareFromContinuingOperations"],
     ["희석주당이익", "희석주당순이익", "희석주당순손익", "기본및희석주당이익", "기본및희석주당순이익",
      "보통주희석주당이익", "보통주희석주당순이익", "보통주희석주당순손익", "보통주기본및희석주당이익"]),

    ("BS", "current_assets", "유동자산", "stock", ["ifrs-full_CurrentAssets"], ["유동자산"]),
    ("BS", "cash", "현금및현금성자산", "stock", ["ifrs-full_CashAndCashEquivalents", "ifrs_CashAndCashEquivalents"],
     ["현금및현금성자산", "현금및현금등가물"]),
    ("BS", "st_fin", "단기금융상품", "stock",
     ["ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents", "dart_ShortTermDepositsNotClassifiedAsCashEquivalents"],
     ["단기금융상품", "단기금융자산", "단기예금"]),
    ("BS", "cur_fin_assets", "유동금융자산", "stock", ["ifrs-full_CurrentFinancialAssets"], ["유동금융자산", "단기금융자산"]),
    ("BS", "lt_fin", "장기금융상품", "stock",
     ["dart_LongTermDepositsNotClassifiedAsCashEquivalents", "ifrs-full_LongtermDepositsNotClassifiedAsCashEquivalents"],
     ["장기금융상품", "장기예금"]),
    ("BS", "noncur_fin_assets", "비유동금융자산", "stock", ["ifrs-full_NoncurrentFinancialAssets"], ["비유동금융자산", "장기금융자산"]),
    ("BS", "trade_receivables", "매출채권", "stock",
     ["ifrs-full_CurrentTradeReceivables", "ifrs-full_TradeAndOtherCurrentReceivables", "dart_ShortTermTradeReceivable"],
     ["매출채권", "매출채권및기타채권", "매출채권및기타유동채권"]),
    ("BS", "inventories", "재고자산", "stock", ["ifrs-full_Inventories"], ["재고자산"]),
    ("BS", "noncurrent_assets", "비유동자산", "stock", ["ifrs-full_NoncurrentAssets"], ["비유동자산"]),
    ("BS", "ppe", "유형자산", "stock", ["ifrs-full_PropertyPlantAndEquipment"], ["유형자산"]),
    ("BS", "intangibles", "무형자산", "stock",
     ["ifrs-full_IntangibleAssetsAndGoodwill", "ifrs-full_IntangibleAssetsOtherThanGoodwill"], ["무형자산", "무형자산및영업권"]),
    ("BS", "investment_property", "투자부동산", "stock", ["ifrs-full_InvestmentProperty"], ["투자부동산"]),
    ("BS", "associates", "관계기업 투자자산", "stock",
     ["ifrs-full_InvestmentAccountedForUsingEquityMethod", "ifrs-full_InvestmentsInAssociates"],
     ["관계기업및공동기업투자", "관계기업투자", "관계기업투자주식", "지분법적용투자주식", "관계기업및공동기업투자자산"]),
    ("BS", "total_assets", "자산총계", "stock", ["ifrs-full_Assets"], ["자산총계"]),
    ("BS", "current_liabilities", "유동부채", "stock", ["ifrs-full_CurrentLiabilities"], ["유동부채"]),
    ("BS", "cur_fin_liab", "유동금융부채", "stock", ["ifrs-full_CurrentFinancialLiabilities"], ["유동금융부채", "단기금융부채"]),
    ("BS", "st_borrow", "단기차입금", "stock", ["ifrs-full_ShorttermBorrowings", "dart_CurrentLoansReceived"], ["단기차입금"]),
    ("BS", "cur_ltd", "유동성장기부채", "stock", ["ifrs-full_CurrentPortionOfLongtermBorrowings"],
     ["유동성장기부채", "유동성장기차입금"]),
    ("BS", "cur_bonds", "유동성사채", "stock", ["ifrs-full_CurrentPortionOfNoncurrentBondsIssued", "dart_CurrentPortionOfBondsIssued"],
     ["유동성사채", "유동성장기사채"]),
    ("BS", "trade_payables", "매입채무", "stock",
     ["ifrs-full_TradeAndOtherCurrentPayablesToTradeSuppliers", "ifrs-full_TradeAndOtherCurrentPayables",
      "dart_ShortTermTradePayables"],
     ["매입채무", "매입채무및기타채무", "매입채무및기타유동채무"]),
    ("BS", "noncurrent_liabilities", "비유동부채", "stock", ["ifrs-full_NoncurrentLiabilities"], ["비유동부채"]),
    ("BS", "noncur_fin_liab", "비유동금융부채", "stock", ["ifrs-full_NoncurrentFinancialLiabilities"], ["비유동금융부채", "장기금융부채"]),
    ("BS", "lt_borrow", "장기차입금", "stock",
     ["ifrs-full_NoncurrentPortionOfNoncurrentLoansReceived", "dart_LongTermBorrowingsGross", "ifrs-full_LongtermBorrowings"],
     ["장기차입금"]),
    ("BS", "bonds", "사채", "stock",
     ["ifrs-full_NoncurrentPortionOfNoncurrentBondsIssued", "dart_BondsIssued", "ifrs-full_BondsIssued"], ["사채"]),
    ("BS", "total_liabilities", "부채총계", "stock", ["ifrs-full_Liabilities"], ["부채총계"]),
    ("BS", "equity_owner", "지배주주지분", "stock", ["ifrs-full_EquityAttributableToOwnersOfParent"],
     ["지배기업소유주지분", "지배기업의소유주에게귀속되는자본", "지배기업의소유주지분"]),
    ("BS", "retained_earnings", "이익잉여금", "stock", ["ifrs-full_RetainedEarnings"], ["이익잉여금", "이익잉여금결손금"]),
    ("BS", "equity_minority", "비지배주주지분", "stock", ["ifrs-full_NoncontrollingInterests"], ["비지배지분"]),
    ("BS", "total_equity", "자본총계", "stock", ["ifrs-full_Equity"], ["자본총계"]),

    ("CF", "cfo", "영업현금흐름", "flow", ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
     ["영업활동현금흐름", "영업활동으로인한현금흐름"]),
    ("CF", "depreciation", "유형자산 감가상각비", "flow",
     ["ifrs-full_AdjustmentsForDepreciationExpense", "ifrs-full_DepreciationExpense",
      "dart_DepreciationExpense"],
     ["감가상각비", "유형자산감가상각비", "유형자산상각비"]),
    ("CF", "amortization", "무형자산 상각비", "flow",
     ["ifrs-full_AdjustmentsForAmortisationExpense", "ifrs-full_AmortisationExpense",
      "dart_AmortisationExpense"],
     ["무형자산상각비", "무형자산상각"]),
    ("CF", "cfi", "투자현금흐름", "flow", ["ifrs-full_CashFlowsFromUsedInInvestingActivities"],
     ["투자활동현금흐름", "투자활동으로인한현금흐름"]),
    ("CF", "capex_ppe", "유형자산 취득", "flow",
     ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"], ["유형자산의취득", "유형자산취득"]),
    ("CF", "capex_intangible", "무형자산 취득", "flow",
     ["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"], ["무형자산의취득", "무형자산취득"]),
    ("CF", "cff", "재무현금흐름", "flow", ["ifrs-full_CashFlowsFromUsedInFinancingActivities"],
     ["재무활동현금흐름", "재무활동으로인한현금흐름"]),
    ("CF", "dividends_paid", "배당금 지급", "flow",
     ["ifrs-full_DividendsPaidClassifiedAsFinancingActivities", "ifrs-full_DividendsPaid"], ["배당금의지급", "배당금지급"]),
    ("CF", "treasury_purchase", "자기주식 취득", "flow",
     ["dart_AcquisitionOfTreasuryShares", "ifrs-full_PaymentsToAcquireOrRedeemEntitysShares"], ["자기주식의취득", "자기주식취득"]),
    ("CF", "cash_change", "현금 증감", "flow", ["ifrs-full_IncreaseDecreaseInCashAndCashEquivalents"],
     ["현금및현금성자산의증가", "현금및현금성자산의순증가", "현금및현금성자산의순증감", "현금의증가"]),
    ("CF", "cash_end", "기말 현금", "stock",
     ["dart_CashAndCashEquivalentsAtEndOfPeriodCf"], ["기말의현금및현금성자산", "기말현금및현금성자산", "기말의현금"]),
]

# 표에 내보내지 않는 보조 항목 (금융부채 대체 계산용)
HIDDEN_KEYS = {"st_borrow", "cur_ltd", "cur_bonds", "lt_borrow", "bonds"}
# ID 가 맞아도 이름에 이 말이 들어가면 대응하지 않음 (삼성전자 2017~2018 "장기매도가능금융자산" 이 장기금융상품 ID 를 씀)
RULE_EXCLUDE_NAMES = {"lt_fin": ["매도가능"]}
FIN_FALLBACK_FORMULAS = {
    "cur_fin_assets": "현금및현금성자산 + 단기금융상품 (유동금융자산 항목 없음)",
    "noncur_fin_assets": "장기금융상품 (비유동금융자산 항목 없음)",
    "cur_fin_liab": "단기차입금 + 유동성장기부채 + 유동성사채 (유동금융부채 항목 없음)",
    "noncur_fin_liab": "장기차입금 + 사채 (비유동금융부채 항목 없음)",
}

# 유형자산 취득 합계가 없을 때 합산하는 구성항목 (account_id 후보, account_nm 후보)
CAPEX_PPE_PARTS = [
    (["dart_PurchaseOfLand"], ["토지의취득", "토지취득"]),
    (["dart_PurchaseOfBuildings"], ["건물의취득", "건물취득"]),
    (["dart_PurchaseOfMachinery"], ["기계장치의취득", "기계장치취득", "기계의취득"]),
    (["dart_PurchaseOfConstructionInProgress"], ["건설중인자산의취득", "건설중인자산취득"]),
]
CAPEX_PPE_PARTS_FORMULA = "토지·건물·기계장치·건설중인자산 취득 합산 (유형자산 취득 합계 없음)"
# 현금 증감이 없을 때 쓰는 기초 현금 (표에는 내보내지 않음)
CASH_BEGIN_RULE = (["dart_CashAndCashEquivalentsAtBeginningOfPeriodCf"],
                   ["기초의현금및현금성자산", "기초현금및현금성자산", "기초의현금"])
CASH_CHANGE_FORMULA = "기말 현금 - 기초 현금 (현금 증감 항목 없음)"
NET_INCOME_FORMULA = "지배주주순이익 + 비지배주주순이익 (당기순이익 항목 없음)"
NET_INCOME_OWNER_ONLY_FORMULA = "지배주주순이익 (당기순이익·비지배주주순이익 항목 없음)"

# 직접 나오지 않으면 계산으로 채우는 항목 (always=True 면 항상 계산)
DERIVED = [
    # (표, 키, 이름, 계산식 설명, always)
    ("IS", "gross_profit", "매출총이익", "매출액 - 매출원가", False),
    ("IS", "sga", "판매관리비", "매출총이익 - 영업이익", False),
    ("CF", "capex", "CAPEX", "유형자산 취득 + 무형자산 취득 (지출 크기, 양수)", True),
    ("CF", "fcf", "FCF", "영업현금흐름 - CAPEX", True),
    ("BS", "fin_assets_total", "금융자산 합계", "현금및현금성자산 + 단기금융상품 + 장기금융상품 (있는 항목의 합)", True),
    ("BS", "fin_liab_total", "금융부채 합계", "유동금융부채 + 비유동금융부채 (있는 항목의 합)", True),
    ("BS", "net_fin_assets", "순금융자산", "금융자산 합계 - 금융부채 합계 (금융부채 없으면 금융자산 합계)", True),
    ("BS", "nwc", "순운전자본", "매출채권 + 재고자산 - 매입채무", True),
]
# 지배/비지배 구분이 없을 때(별도 재무제표, 또는 연결인데 구분이 없는 회사) 대신 쓰는 값
#   키: (대신 쓸 항목, 구분이 없다고 보는 조건 = 이 비지배 항목도 없음, 표시 문구)
OWNER_FALLBACK = {
    "net_income_owner": ("net_income", "net_income_minority", "지배/비지배 구분 없음: 당기순이익"),
    "equity_owner": ("total_equity", "equity_minority", "지배/비지배 구분 없음: 자본총계"),
}

SHEET_ORDER = ["IS", "BS", "CF"]
SHEET_NAMES = {"IS": "손익계산서", "BS": "재무상태표", "CF": "현금흐름표"}


class TransientError(Exception):
    """호출 한도 초과·점검·통신 오류 — 저장하지 않는다."""


# ===== 공통 =====
_api_lock = threading.Lock()
_last_call = [0.0]
_code_locks = {}
_code_locks_guard = threading.Lock()


def _code_lock(code):
    with _code_locks_guard:
        return _code_locks.setdefault(code, threading.Lock())


_tls = threading.local()


def _session():
    # 연결 재사용 (DART 응답이 느려 매 호출 TLS 연결을 새로 맺는 비용을 줄임)
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = requests.Session()
    return s


def _api_key():
    return os.environ.get("DART_API_KEY") or ""


def _dart_get(endpoint, params):
    """DART 호출 (간격 유지 + 재시도). 오류 메시지에 주소(키 포함)를 남기지 않는다."""
    p = dict(params, crtfc_key=_api_key())
    delay = 0.5
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        with _api_lock:
            wait = MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            res = _session().get(DART_URL + endpoint, params=p, timeout=20)
            if res.status_code != 200:
                last = f"HTTP {res.status_code}"
            else:
                d = res.json()
                st = d.get("status")
                if st in ("000", "013"):
                    return d
                last = f"DART status {st} {d.get('message', '')}".strip()
                if st in ("010", "011", "012", "020", "021", "101"):   # 키·권한·한도 문제는 재시도해도 같음
                    break
        except Exception as e:   # requests 예외 문자열에는 키가 든 주소가 들어 있어 이름만 남긴다
            last = type(e).__name__
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    raise TransientError(f"{endpoint} {params.get('bsns_year', '')} {params.get('reprt_code', '')}: {last}".strip())


def _atomic_write(path, data):
    """gzip 으로 임시 파일에 쓴 뒤 os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"{code}.json.gz")


def _legacy_path(code):
    return os.path.join(CACHE_DIR, f"{code}.json")   # 1단계-B 형식 (비압축, 자본변동표 포함)


def _strip_sce(cache):
    i = ROW_FIELDS.index("sj_div")
    for r in cache.get("reports", {}).values():
        if r.get("rows"):
            r["rows"] = [x for x in r["rows"] if x[i] != "SCE"]
    return cache


def _migrate_legacy(code):
    """이전 형식 파일을 새 형식으로 옮긴다(호출 쪽에서 종목 잠금). 반환: (이전 크기, 새 크기) 또는 None"""
    old = _legacy_path(code)
    if not os.path.exists(old):
        return None
    with open(old, "r", encoding="utf-8") as f:
        cache = json.load(f)
    before = os.path.getsize(old)
    _atomic_write(_cache_path(code), _strip_sce(cache))
    os.remove(old)
    return before, os.path.getsize(_cache_path(code))


def migrate_all_legacy():
    """저장된 이전 형식 파일 전부를 새 형식으로. 반환: {종목코드: (이전 크기, 새 크기)}"""
    out = {}
    if not os.path.isdir(CACHE_DIR):
        return out
    for fn in sorted(os.listdir(CACHE_DIR)):
        if fn.endswith(".json") and not fn.startswith("."):
            code = fn[:-5]
            with _code_lock(code):
                try:
                    r = _migrate_legacy(code)
                except (OSError, json.JSONDecodeError) as e:
                    print(f"[kr_financials] {code} 형식 변환 실패: {type(e).__name__}", flush=True)
                    continue
            if r:
                out[code] = r
    return out


def _load_cache(code):
    try:
        with gzip.open(_cache_path(code), "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, EOFError, json.JSONDecodeError):
        return None
    try:
        with open(_legacy_path(code), "r", encoding="utf-8") as f:
            return _strip_sce(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _month_end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_months(y, m, delta):
    t = y * 12 + (m - 1) + delta
    return t // 12, t % 12 + 1


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ===== 기간 =====
def _fy_end_of(d, acc_mt):
    """날짜 d 가 속한 회계연도의 말일"""
    y = d.year if d.month <= acc_mt else d.year + 1
    return _month_end(y, acc_mt)


def _report_plan(fy_end, acc_mt):
    """회계연도 → [(키, reprt_code, bsns_year, 기간 말일, 공시 기한 일수)]"""
    out = []
    for key, rc, back, deadline in REPORTS:
        y, m = _add_months(fy_end.year, fy_end.month, -back)
        if acc_mt == 12 or key == "FY":
            bsns_year = fy_end.year
        else:
            bsns_year = fy_end.year - 1
        out.append((key, rc, bsns_year, _month_end(y, m), deadline))
    return out


def _label(d):
    return f"{d.year}.{d.month:02d}"


# ===== 받아오기 =====
def _compact_rows(rows):
    return [[r.get(f, "") for f in ROW_FIELDS] for r in rows if r.get("sj_div") != "SCE"]


def _fetch_report(corp_code, bsns_year, reprt_code):
    """반환: {"status": "ok", "fs_div", "rcept_no", "rows"} 또는 {"status": "nodata"}. 일시 오류는 TransientError"""
    for fs in ("CFS", "OFS"):
        d = _dart_get("fnlttSinglAcntAll.json",
                      {"corp_code": corp_code, "bsns_year": str(bsns_year), "reprt_code": reprt_code, "fs_div": fs})
        rows = d.get("list") or []
        if d.get("status") == "000" and rows:
            return {"status": "ok", "fs_div": fs, "rcept_no": rows[0].get("rcept_no", ""),
                    "currency": rows[0].get("currency", ""), "rows": _compact_rows(rows)}
    return {"status": "nodata"}


def _fetch_shares(corp_code, bsns_year, reprt_code):
    """DART 주식총수(stockTotqySttus): 보통주 발행·자기주식·유통주식수. 값이 없는 보고서는 nodata."""
    d = _dart_get("stockTotqySttus.json", {"corp_code": corp_code, "bsns_year": str(bsns_year), "reprt_code": reprt_code})
    rows = d.get("list") or []
    by_se = {}
    for r in rows:
        se = (r.get("se") or "").strip()
        if se in ("보통주", "합계") and se not in by_se:
            by_se[se] = r
    out = {"status": "nodata", "stlm_dt": (rows[0].get("stlm_dt") if rows else None)}
    for se, key in (("보통주", "common"), ("합계", "total")):
        r = by_se.get(se)
        if not r:
            continue
        issued, treasury, distrib = _amount(r.get("istc_totqy")), _amount(r.get("tesstk_co")), _amount(r.get("distb_stock_co"))
        if distrib is None and issued is not None:
            distrib = issued - (treasury or 0)
        if distrib is not None:
            out[key] = distrib
            out[key + "_issued"] = issued
            out[key + "_treasury"] = treasury or 0
            out["status"] = "ok"
            out["stlm_dt"] = r.get("stlm_dt") or out["stlm_dt"]
    if out["status"] == "ok" and "common" not in out and "total" in out:
        out["common"] = out["total"]         # 보통주 행이 없으면 합계
    return out


def _shares_backlog(cache):
    """재무제표는 받았는데 주식총수는 아직 없는 보고서"""
    sh = cache.get("shares", {})
    return [rid for rid, r in cache.get("reports", {}).items() if r.get("status") == "ok" and rid not in sh]


def _ensure_acc_mt(cache, corp_code):
    checked = cache.get("acc_mt_checked")
    if cache.get("acc_mt") and checked and (date.today() - date.fromisoformat(checked)).days < ACC_MT_TTL_DAYS:
        return False
    d = _dart_get("company.json", {"corp_code": corp_code})
    mt = (d.get("acc_mt") or "").strip()
    if not mt.isdigit() or not 1 <= int(mt) <= 12:
        if cache.get("acc_mt"):
            return False
        raise TransientError("company.json: 결산월(acc_mt) 없음")
    cache["acc_mt"] = int(mt)
    cache["acc_mt_checked"] = date.today().isoformat()
    cache["corp_name_dart"] = d.get("corp_name", "")
    return True


def _need_fetch(entry, period_end, deadline_days, today):
    if period_end >= today:
        return False                        # 기간이 아직 끝나지 않음
    if not entry:
        return True
    if entry.get("status") == "ok":
        return False                        # 정상 응답은 계속 보관
    if entry.get("final"):
        return False                        # 공시 기한이 지난 "자료 없음"
    return entry.get("checked", "") < today.isoformat()   # 최근 기간 "자료 없음": 하루 한 번 다시 확인


def _plan_fetch(cache, years, today):
    """받아야 할 보고서 목록 [(rid, bsns_year, reprt_code, 기간 말일, 공시 기한)] (결산월이 있어야 함)"""
    acc_mt = cache["acc_mt"]
    fy_last = _fy_end_of(today, acc_mt)
    reports = cache.get("reports", {})
    out = []
    for i in range(years + 1):   # 진행 중인 회계연도 + 끝난 회계연도 years 개
        fy_end = _month_end(fy_last.year - i, acc_mt)
        for key, rc, by, pend, deadline in _report_plan(fy_end, acc_mt):
            rid = f"{by}_{rc}"
            if _need_fetch(reports.get(rid), pend, deadline, today):
                out.append((rid, by, rc, pend, deadline))
    return out


def needs_fetch(code, years=DEFAULT_YEARS):
    """저장본만 보고 받아올 것이 있는지 (호출 없음)."""
    cache = _load_cache(code)
    if not cache or not cache.get("acc_mt"):
        return True
    checked = cache.get("acc_mt_checked")
    if not checked or (date.today() - date.fromisoformat(checked)).days >= ACC_MT_TTL_DAYS:
        return True
    return bool(_plan_fetch(cache, max(1, min(MAX_YEARS, int(years))), date.today())) or bool(_shares_backlog(cache))


def refresh(code, corp_code, years=DEFAULT_YEARS, progress=None):
    """필요한 보고서만 받아 저장. progress(끝난 수, 전체 수) 콜백. 반환: (cache, warnings)
    결산월을 못 받으면 TransientError. 중간 실패는 받은 것까지 저장하고 warnings 에 남긴다."""
    years = max(1, min(MAX_YEARS, int(years)))
    warnings = []
    with _code_lock(code):
        changed = _migrate_legacy(code) is not None
        cache = _load_cache(code) or {"code": code, "corp_code": corp_code, "reports": {}}
        cache["corp_code"] = corp_code
        changed = _ensure_acc_mt(cache, corp_code) or changed
        today = date.today()
        reports = cache.setdefault("reports", {})
        shares = cache.setdefault("shares", {})
        plan = _plan_fetch(cache, years, today)
        backlog = _shares_backlog(cache)
        total = len(plan) * 2 + len(backlog)   # 보고서마다 재무제표 + 주식총수
        done = 0
        if progress:
            progress(0, total)
        try:
            for rid, by, rc, pend, deadline in plan:
                r = _fetch_report(corp_code, by, rc)
                r["fetched_at"] = _now()
                if r["status"] == "nodata":
                    r["checked"] = today.isoformat()
                    r["final"] = (today - pend).days > deadline + FILING_GRACE_DAYS
                reports[rid] = r
                changed = True
                done += 1
                if progress:
                    progress(done, total)
                if r["status"] == "ok":
                    shares[rid] = _fetch_shares(corp_code, by, rc)
                    shares[rid]["fetched_at"] = _now()
                done += 1
                if progress:
                    progress(done, total)
            for rid in backlog:
                by, rc = rid.split("_")
                shares[rid] = _fetch_shares(corp_code, by, rc)
                shares[rid]["fetched_at"] = _now()
                changed = True
                done += 1
                if progress:
                    progress(done, total)
        except TransientError as e:
            warnings.append(f"일부 보고서를 받지 못함(저장 안 함, 다음 조회 때 다시 시도): {e}")
        if changed:
            cache["updated_at"] = _now()
            _atomic_write(_cache_path(code), cache)
    return cache, warnings


# ===== 뒤에서 받아오기 (조회 API 는 기다리지 않음) =====
_jobs = {}            # 종목코드 → {"state": running|done|failed, "done", "total", "error", "warnings", ...}
_jobs_guard = threading.Lock()


def _run_job(code, corp_code, years, job):
    def prog(done, total):
        job["done"], job["total"] = done, total
    try:
        _, warns = refresh(code, corp_code, years, progress=prog)
        job["warnings"] = warns
        job["state"] = "failed" if warns else "done"
        if warns:
            job["error"] = warns[0]
    except TransientError as e:
        job["state"], job["error"] = "failed", str(e)
    except Exception as e:   # 예상 못 한 오류도 서버를 멈추지 않게
        job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
    job["finished_at"] = _now()


def start_fetch(code, corp_code, years=DEFAULT_YEARS):
    """받아오기 작업을 뒤에서 시작(이미 진행 중이면 그 작업). 반환: 작업 정보 dict"""
    with _jobs_guard:
        job = _jobs.get(code)
        if job and job["state"] == "running":
            return job
        job = {"state": "running", "done": 0, "total": None, "error": None, "warnings": [],
               "years": years, "started_at": _now(), "finished_at": None}
        job["thread"] = threading.Thread(target=_run_job, args=(code, corp_code, years, job), daemon=True)
        _jobs[code] = job
        job["thread"].start()
        return job


def fetch_blocking(code, corp_code, years=DEFAULT_YEARS):
    """미리 받기용: 작업을 시작(또는 진행 중인 작업에 합류)하고 끝날 때까지 기다린다."""
    job = start_fetch(code, corp_code, years)
    job["thread"].join()
    return job


def job_progress(job):
    return {"done": job.get("done", 0), "total": job.get("total")}


def get_financials_nowait(code, corp_code, name="", years=5):
    """저장본이 있으면 바로 계산해 돌려주고, 없으면 뒤에서 받아오며 진행률을 돌려준다.
    status: ready | fetching | failed. ready 이면서 뒤에서 갱신 중이면 updating 에 진행률."""
    years = max(1, min(MAX_YEARS, int(years)))
    years_fetch = max(DEFAULT_YEARS, years)
    with _jobs_guard:
        job = _jobs.get(code)
        failed = None
        if job and job["state"] in ("done", "failed"):
            _jobs.pop(code, None)               # 끝난 작업 정보는 한 번만 전달 → 다음 요청은 다시 시도 가능
            if job["state"] == "failed":
                failed = job
            job = None
    cache = _load_cache(code)
    usable = bool(cache and cache.get("acc_mt"))
    if job:                                      # 진행 중: 받아둔 것이 있으면 먼저 보여주고 진행률을 함께
        if usable and any(r.get("status") == "ok" for r in cache.get("reports", {}).values()):
            out = compute(cache, years, name)
            out.update(status="ready", updating=job_progress(job))
            return out
        return {"status": "fetching", "market": "KR", "code": code, "name": name, "progress": job_progress(job)}
    if failed and not usable:
        return {"status": "failed", "market": "KR", "code": code, "name": name, "error": failed["error"]}
    if not usable:
        job = start_fetch(code, corp_code, years_fetch)
        return {"status": "fetching", "market": "KR", "code": code, "name": name, "progress": job_progress(job)}
    out = compute(cache, years, name)
    out["status"] = "ready"
    out["updating"] = None
    if failed:                                   # 받아둔 것으로 보여주되 실패 사유를 알림 (자동 재시도는 다음 요청에서)
        out["fetch_error"] = failed["error"]
        out["warnings"] = [failed["error"]] + out["warnings"]
    elif needs_fetch(code, years_fetch):
        job = start_fetch(code, corp_code, years_fetch)
        out["updating"] = job_progress(job)
    return out


# ===== 항목 대응 =====
_NUM_PREFIX = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ0-9]+[.\s]*")


def norm_name(s):
    s = re.sub(r"\s+", "", s or "")
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = _NUM_PREFIX.sub("", s)
    s = re.sub(r"^연결(?=당|분기|반기)", "", s)          # 연결당기순이익·연결분기순이익 (현대차 2021~2023 등)
    s = re.sub(r"^(당분기|당반기|분기|반기)", "당기", s)
    s = re.sub(r"주당(당기|분기|반기)순", "주당순", s)
    return s


def _amount(v):
    v = (v or "").replace(",", "").strip()
    if v in ("", "-"):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _sheet_rows(rows, sheet):
    idx = ROW_FIELDS.index("sj_div")
    if sheet == "IS":
        r = [x for x in rows if x[idx] == "IS"]
        return r or [x for x in rows if x[idx] == "CIS"]   # 단일 포괄손익계산서 회사
    return [x for x in rows if x[idx] == sheet]


def match_rule(rows, ids, names):
    """account_id 먼저, 없으면 account_nm. 세부(account_detail) 행은 뒤로."""
    I = {f: i for i, f in enumerate(ROW_FIELDS)}
    main = sorted(rows, key=lambda x: (x[I["account_detail"]] not in ("", "-"), int(x[I["ord"]] or 0)
                                       if str(x[I["ord"]]).isdigit() else 0))
    for aid in ids:
        for x in main:
            if x[I["account_id"]] == aid:
                return x, "id"
    for nm in names:
        for x in main:
            if norm_name(x[I["account_nm"]]) == nm:
                return x, "name"
    return None, None


def extract(report):
    """보고서 하나 → ({키: (thstrm_amount, thstrm_add_amount)}, {계산으로 채운 키: 계산식})"""
    I = {f: i for i, f in enumerate(ROW_FIELDS)}
    rows = report.get("rows") or []
    sheets = {s: _sheet_rows(rows, s) for s in SHEET_ORDER}
    out, calc = {}, {}

    def val(x):
        return (_amount(x[I["thstrm_amount"]]), _amount(x[I["thstrm_add_amount"]]))

    for sheet, key, label, kind, ids, names in RULES:
        x, _ = match_rule(sheets[sheet], ids, names)
        if x is not None and any(w in x[I["account_nm"]] for w in RULE_EXCLUDE_NAMES.get(key, [])):
            x = None
        if x is not None:
            out[key] = val(x)

    def _stock_sum(keys):   # 시점 값 합 (있는 항목만, 하나도 없으면 None)
        xs = [out[k][0] for k in keys if k in out and out[k][0] is not None]
        return sum(xs) if xs else None

    # 금융자산·금융부채 대체 (합계 항목이 없을 때)
    for key, parts in (("cur_fin_assets", ["cash", "st_fin"]), ("noncur_fin_assets", ["lt_fin"]),
                       ("cur_fin_liab", ["st_borrow", "cur_ltd", "cur_bonds"]), ("noncur_fin_liab", ["lt_borrow", "bonds"])):
        if key not in out:
            v = _stock_sum(parts)
            if v is not None:
                out[key] = (v, None)
                calc[key] = FIN_FALLBACK_FORMULAS[key]
    # 유형자산 취득 합계가 없으면 구성항목 합산
    if "capex_ppe" not in out:
        parts = [match_rule(sheets["CF"], ids, names)[0] for ids, names in CAPEX_PPE_PARTS]
        amts = [_amount(x[I["thstrm_amount"]]) for x in parts if x is not None]
        amts = [a for a in amts if a is not None]
        if amts:
            out["capex_ppe"] = (sum(abs(a) for a in amts), None)
            calc["capex_ppe"] = CAPEX_PPE_PARTS_FORMULA
    # 당기순이익이 없으면 지배주주순이익 + 비지배주주순이익 (3개월·누적 각각)
    if "net_income" not in out and "net_income_owner" in out and "net_income_minority" in out:
        o, m = out["net_income_owner"], out["net_income_minority"]
        out["net_income"] = (_add(o[0], m[0]), _add(o[1], m[1]))
        if out["net_income"][0] is not None:
            calc["net_income"] = NET_INCOME_FORMULA
        else:
            del out["net_income"]
    elif "net_income" not in out and "net_income_owner" in out and "net_income_minority" not in out:
        out["net_income"] = out["net_income_owner"]
        calc["net_income"] = NET_INCOME_OWNER_ONLY_FORMULA
    # 현금 증감이 없으면 기말 현금 - 기초 현금 (누적 기간 기준 → 분기 값은 기존 차감 규칙으로)
    if "cash_change" not in out and out.get("cash_end", (None,))[0] is not None:
        xb, _ = match_rule(sheets["CF"], *CASH_BEGIN_RULE)
        b = _amount(xb[I["thstrm_amount"]]) if xb is not None else None
        if b is not None:
            out["cash_change"] = (out["cash_end"][0] - b, None)
            calc["cash_change"] = CASH_CHANGE_FORMULA
    return out, calc


# ===== 계산 =====
def _sub(a, b):
    return None if a is None or b is None else a - b


def _add(*xs):
    return None if any(x is None for x in xs) else sum(xs)


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(a / b * 100, 2)


def _ranges(all_labels, gaps):
    """연속한 빈 기간을 "2021.03~2022.12" 식 범위로"""
    idx = {lb: i for i, lb in enumerate(all_labels)}
    out, run = [], []
    for lb in gaps:
        if run and idx[lb] != idx[run[-1]] + 1:
            out.append(run[0] if len(run) == 1 else f"{run[0]}~{run[-1]}")
            run = []
        run.append(lb)
    if run:
        out.append(run[0] if len(run) == 1 else f"{run[0]}~{run[-1]}")
    return out


def _growth(cur, prev):
    if cur is None or prev is None or prev <= 0:
        return None
    return round((cur - prev) / prev * 100, 2)


def _shares_for(cache, rid, period_end):
    """기간 말 유통주식수. 그 보고서에 공시가 있으면 source=dart, 없으면 직전 공시값을 이월(source=carry, 원래 공시일 from).
    직전 공시값도 없으면 None → 화면이 현재 주식수로 근사."""
    shares = cache.get("shares") or {}

    def _info(sh, source, src_dt=None):
        dt = (sh.get("stlm_dt") or "")[:10]
        return {"common": sh["common"], "total": sh.get("total"), "issued": sh.get("common_issued"),
                "treasury": sh.get("common_treasury"), "stlm_dt": dt, "source": source, "from": src_dt or dt}

    sh = shares.get(rid or "")
    if sh and sh.get("status") == "ok" and sh.get("common") is not None:
        dt = (sh.get("stlm_dt") or "")[:10]
        if not dt or dt == period_end.isoformat():
            return _info(sh, "dart")
    # 직전 공시값 이월: 결산기준일이 기간 말보다 앞선 것 중 가장 최근
    pe = period_end.isoformat()
    prev = [x for x in shares.values()
            if x.get("status") == "ok" and x.get("common") is not None and (x.get("stlm_dt") or "")[:10] < pe]
    if not prev:
        return None
    best = max(prev, key=lambda x: x.get("stlm_dt") or "")
    return _info(best, "carry", (best.get("stlm_dt") or "")[:10])


def compute(cache, years=5, name=""):
    acc_mt = cache["acc_mt"]
    reports = cache.get("reports", {})
    today = date.today()
    fy_last = _fy_end_of(today, acc_mt)
    warnings = []
    kinds = {key: (sheet, kind) for sheet, key, label, kind, ids, names in RULES}

    # 회계연도별 보고서 값
    n_fy = MAX_YEARS + 1
    fys = []
    for i in range(n_fy - 1, -1, -1):
        fy_end = _month_end(fy_last.year - i, acc_mt)
        plan = _report_plan(fy_end, acc_mt)
        reps = {}
        for key, rc, by, pend, deadline in plan:
            r = reports.get(f"{by}_{rc}")
            if r and r.get("status") == "ok":
                v, calc = extract(r)
                reps[key] = {"v": v, "calc": calc, "fs": r["fs_div"], "rcept": r.get("rcept_no", ""), "end": pend}
                rd = r.get("rcept_no", "")[:8]
                if len(rd) == 8 and rd <= pend.strftime("%Y%m%d"):
                    warnings.append(f"{_label(pend)} 보고서 접수일({rd})이 기간 말보다 빠름 — 기간 대응 확인 필요")
        fys.append({"fy_end": fy_end, "plan": plan, "reps": reps})

    # 분기 값
    quarters = []   # [{"end", "fs", "vals": {키: 값}}]
    annual = []     # [{"end", "fs", "vals"}]
    for fy in fys:
        reps = fy["reps"]
        fs_set = {r["fs"] for r in reps.values()}
        mixed = len(fs_set) > 1
        if mixed:
            warnings.append(f"회계연도 {_label(fy['fy_end'])}: 연결/별도가 섞여 있어 분기 차감 계산을 하지 않음")
        g = lambda k, key: (reps[k]["v"].get(key) if k in reps else None)
        qv = {k: {} for k in ("Q1", "H1", "Q3", "FY")}
        for key, (sheet, kind) in kinds.items():
            r1, r2, r3, r4 = g("Q1", key), g("H1", key), g("Q3", key), g("FY", key)
            a = lambda r: r[0] if r else None
            ad = lambda r: r[1] if r else None
            if kind == "stock":
                q = [a(r1), a(r2), a(r3), a(r4)]
            elif sheet == "CF":   # 현금흐름은 누적 값만 → 차이
                c = [a(r1), a(r2), a(r3), a(r4)]
                q = [c[0]] + ([None] * 3 if mixed else [_sub(c[1], c[0]), _sub(c[2], c[1]), _sub(c[3], c[2])])
            else:                 # 손익: 3개월 값, 4분기 = 연간 - 3분기누적
                q1 = a(r1) if a(r1) is not None else ad(r1)
                q2 = a(r2)
                q3 = a(r3)
                c2 = ad(r2) if ad(r2) is not None else _add(q1, q2)
                if q2 is None and not mixed:
                    q2 = _sub(ad(r2), q1)
                if q3 is None and not mixed:
                    q3 = _sub(ad(r3), c2)
                c3 = ad(r3) if ad(r3) is not None else _add(c2, q3)
                # 4분기 = 연간 - (1~3분기). 분기 값이 빠졌을 때만 연간 - 3분기누적
                q123 = _add(q1, q2, q3)
                q4 = None if mixed else _sub(a(r4), q123 if q123 is not None else c3)
                q = [q1, q2, q3, q4]
            for k, v in zip(("Q1", "H1", "Q3", "FY"), q):
                if v is not None:
                    qv[k][key] = v
        # 보고서 단계에서 계산으로 채운 값: 그 분기 값에 쓰인 보고서(그 분기까지의 보고서) 기준으로 표시
        order = ["Q1", "H1", "Q3", "FY"]
        for idx, (k, rc, by, pend, dl) in enumerate(fy["plan"]):
            rcalc = {}
            used = []   # 이 분기 값에 쓰인 보고서 (접수번호) — 공시 시점 판정용
            for kk in order[:idx + 1]:
                if kk in reps:
                    rcalc.update(reps[kk]["calc"])
                    used.append(reps[kk]["rcept"])
            avail = max((u[:8] for u in used if len(u) >= 8), default=None)
            quarters.append({"end": pend, "fs": reps[k]["fs"] if k in reps else (reps["FY"]["fs"] if "FY" in reps else None),
                             "vals": qv[k], "rcalc": rcalc, "rid": f"{by}_{rc}",
                             "rcept": reps[k]["rcept"] if k in reps else None, "used": used, "avail": avail})
        annual.append({"end": fy["fy_end"], "fs": reps["FY"]["fs"] if "FY" in reps else None,
                       "rid": next((f"{by}_{rc}" for (kk, rc, by, pend, dl) in fy["plan"] if kk == "FY"), None),
                       "vals": {key: g("FY", key)[0] for key in kinds if g("FY", key) and g("FY", key)[0] is not None},
                       "rcalc": dict(reps["FY"]["calc"]) if "FY" in reps else {}})

    # 4분기누적: 손익·현금흐름 = 최근 4개 분기 합, 재무상태표·시점값 = 분기 말 값
    ttm = []
    for i, q in enumerate(quarters):
        vals = {}
        last4 = quarters[i - 3:i + 1] if i >= 3 else []
        for key, (sheet, kind) in kinds.items():
            if kind == "stock":
                if key in q["vals"]:
                    vals[key] = q["vals"][key]
            elif last4:
                xs = [x["vals"].get(key) for x in last4]
                if all(x is not None for x in xs):
                    vals[key] = sum(xs)
        rcalc = {}
        for x in (last4 or [q]):
            rcalc.update(x["rcalc"])
        ttm.append({"end": q["end"], "fs": q["fs"], "vals": vals, "rcalc": rcalc})

    # 계산 항목
    calculated = {}

    def mark(key, view, label, formula):
        c = calculated.setdefault(key, {"formula": formula, "periods": {"quarter": [], "ttm": [], "annual": []}})
        c["periods"][view].append(label)

    for view, series in (("quarter", quarters), ("ttm", ttm), ("annual", annual)):
        for p in series:
            v, lb = p["vals"], _label(p["end"])
            for key, formula in p.get("rcalc", {}).items():
                if v.get(key) is not None:
                    mark(key, view, lb, formula)
            if "gross_profit" not in v and v.get("revenue") is not None and v.get("cogs") is not None:
                v["gross_profit"] = v["revenue"] - v["cogs"]
                mark("gross_profit", view, lb, "매출액 - 매출원가")
            if "sga" not in v and v.get("gross_profit") is not None and v.get("operating_income") is not None:
                v["sga"] = v["gross_profit"] - v["operating_income"]
                mark("sga", view, lb, "매출총이익 - 영업이익")
            if v.get("cash") is not None:
                v["fin_assets_total"] = v["cash"] + (v.get("st_fin") or 0) + (v.get("lt_fin") or 0)
                mark("fin_assets_total", view, lb, "현금및현금성자산 + 단기금융상품 + 장기금융상품 (있는 항목의 합)")
            if v.get("cur_fin_liab") is not None or v.get("noncur_fin_liab") is not None:
                v["fin_liab_total"] = (v.get("cur_fin_liab") or 0) + (v.get("noncur_fin_liab") or 0)
                mark("fin_liab_total", view, lb, "유동금융부채 + 비유동금융부채 (있는 항목의 합)")
            if v.get("fin_assets_total") is not None:
                v["net_fin_assets"] = v["fin_assets_total"] - (v.get("fin_liab_total") or 0)
                mark("net_fin_assets", view, lb, "금융자산 합계 - 금융부채 합계 (금융부채 없으면 금융자산 합계)")
            if v.get("trade_receivables") is not None and v.get("inventories") is not None and v.get("trade_payables") is not None:
                v["nwc"] = v["trade_receivables"] + v["inventories"] - v["trade_payables"]
                mark("nwc", view, lb, "매출채권 + 재고자산 - 매입채무")
            if v.get("capex_ppe") is not None:
                v["capex"] = abs(v["capex_ppe"]) + abs(v.get("capex_intangible") or 0)
                mark("capex", view, lb, "유형자산 취득 + 무형자산 취득 (지출 크기, 양수)")
                if v.get("cfo") is not None:
                    v["fcf"] = v["cfo"] - v["capex"]
                    mark("fcf", view, lb, "영업현금흐름 - CAPEX")
            for key, (src, minority, formula) in OWNER_FALLBACK.items():
                if key not in v and minority not in v and v.get(src) is not None:
                    v[key] = v[src]
                    mark(key, view, lb, formula)

    # 출력 범위
    years = max(1, min(MAX_YEARS, int(years)))
    def has(p):
        return any(k in p["vals"] for k in ("revenue", "operating_income", "net_income", "total_assets"))
    q_out = [p for p in quarters if p["end"] < today and has(p)][-years * 4:]
    a_out = [p for p in annual if p["end"] < today and has(p)][-years:]
    q_set = {p["end"] for p in q_out}
    t_out = [p for p in ttm if p["end"] in q_set and has(p)]

    # 지표
    def metrics(series, view):
        out = {}
        by_end = {p["end"]: p for p in series}
        ends = [p["end"] for p in series]
        for i, p in enumerate(series):
            v = p["vals"]
            m = {
                "operating_margin": _pct(v.get("operating_income"), v.get("revenue")),
                "net_margin": _pct(v.get("net_income"), v.get("revenue")),
                "debt_ratio": _pct(v.get("total_liabilities"), v.get("total_equity")),
            }
            # 1년 전 같은 기간
            y, mth = _add_months(p["end"].year, p["end"].month, -12)
            prev = by_end.get(_month_end(y, mth))
            pv = prev["vals"] if prev else {}
            for key, name_ in (("revenue", "revenue_yoy"), ("operating_income", "operating_income_yoy"),
                               ("net_income", "net_income_yoy")):
                m[name_] = _growth(v.get(key), pv.get(key))
            if view in ("ttm", "annual"):
                e0, e1 = pv.get("equity_owner"), v.get("equity_owner")
                avg = (e0 + e1) / 2 if e0 is not None and e1 is not None else None
                m["roe"] = _pct(v.get("net_income_owner"), avg) if avg and avg > 0 else None
            out[_label(p["end"])] = m
        return out

    full_q = {p["end"]: p for p in quarters}
    full_t = {p["end"]: p for p in ttm}
    full_a = {p["end"]: p for p in annual}
    # 증감률·ROE 는 출력 범위 밖의 1년 전 값도 써야 하므로 전체 시계열로 계산 후 잘라낸다
    mq = metrics(quarters, "quarter")
    mt = metrics(ttm, "ttm")
    ma = metrics(annual, "annual")
    metrics_out = {
        "quarter": {_label(p["end"]): mq[_label(p["end"])] for p in q_out},
        "ttm": {_label(p["end"]): mt[_label(p["end"])] for p in t_out},
        "annual": {_label(p["end"]): ma[_label(p["end"])] for p in a_out},
    }

    # 표
    labels = {key: label for sheet, key, label, kind, ids, names in RULES}
    labels.update({key: label for sheet, key, label, f, a in DERIVED})
    sheet_keys = {s: [] for s in SHEET_ORDER}
    for sheet, key, label, kind, ids, names in RULES:
        if key in HIDDEN_KEYS:
            continue
        sheet_keys[sheet].append(key)
        if key == "capex_intangible":
            sheet_keys[sheet].append("capex")
        if key == "noncur_fin_assets":
            sheet_keys[sheet].append("fin_assets_total")
        if key == "noncur_fin_liab":
            sheet_keys[sheet].append("fin_liab_total")
            sheet_keys[sheet].append("net_fin_assets")
        if key == "trade_payables":
            sheet_keys[sheet].append("nwc")
    sheet_keys["CF"].append("fcf")

    statements = {}
    missing = []
    partial = []      # 일부 기간만 빈 항목
    for s in SHEET_ORDER:
        items = {}
        for key in sheet_keys[s]:
            q = {_label(p["end"]): p["vals"].get(key) for p in q_out}
            t = {_label(p["end"]): p["vals"].get(key) for p in t_out}
            a = {_label(p["end"]): p["vals"].get(key) for p in a_out}
            if all(x is None for x in list(q.values()) + list(t.values()) + list(a.values())):
                missing.append({"sheet": s, "key": key, "label": labels[key]})
            items[key] = {"label": labels[key], "quarter": q, "ttm": t, "annual": a}
            if not all(x is None for x in list(q.values()) + list(t.values()) + list(a.values())):
                for view, ser in (("quarter", q), ("ttm", t), ("annual", a)):
                    gaps = [lb for lb, x in ser.items() if x is None]
                    if gaps:
                        partial.append({"sheet": s, "key": key, "label": labels[key], "view": view,
                                        "periods": gaps, "ranges": _ranges(list(ser), gaps)})
        statements[s] = {"name": SHEET_NAMES[s], "items": items, "order": list(sheet_keys[s])}

    # 기간 밖 계산 표시는 잘라냄
    keep = {"quarter": {_label(p["end"]) for p in q_out}, "ttm": {_label(p["end"]) for p in t_out},
            "annual": {_label(p["end"]) for p in a_out}}
    for c in calculated.values():
        for view in c["periods"]:
            c["periods"][view] = [lb for lb in c["periods"][view] if lb in keep[view]]
    calculated = {k: v for k, v in calculated.items() if any(v["periods"].values())}

    fs_latest = next((p["fs"] for p in reversed(q_out) if p["fs"]), None)
    fs_used = sorted({p["fs"] for p in q_out + a_out if p["fs"]})
    if len(fs_used) > 1:
        warnings.append(f"기간에 따라 연결/별도가 다름: {fs_used} (최근 기준 {fs_latest})")
    # 조회 범위 안의 "자료 없음" 최근 기간
    for fy in fys:
        for key, rc, by, pend, dl in fy["plan"]:
            r = reports.get(f"{by}_{rc}")
            if r and r.get("status") == "nodata" and not r.get("final") and (today - pend).days > dl:
                warnings.append(f"{_label(pend)} 보고서 아직 없음 (공시 기한 {dl}일이 지났음, 하루 한 번 다시 확인)")

    fetched = [r.get("fetched_at", "") for r in reports.values() if r.get("status") == "ok"]
    return {
        "market": "KR",
        "code": cache["code"],
        "name": name or cache.get("corp_name_dart", ""),
        "fs_div": fs_latest,
        "fs_div_label": {"CFS": "연결", "OFS": "별도"}.get(fs_latest),
        "fs_div_by_period": {
            "quarter": {_label(p["end"]): p["fs"] for p in q_out},
            "annual": {_label(p["end"]): p["fs"] for p in a_out},
        },
        # 기간 말 유통주식수 (DART 주식총수, 보통주). 값이 없는 기간은 None → 화면이 현재 주식수로 근사
        "shares_by_period": {
            "quarter": {_label(p["end"]): _shares_for(cache, p.get("rid"), p["end"]) for p in q_out},
            "annual": {_label(p["end"]): _shares_for(cache, p.get("rid"), p["end"]) for p in a_out},
        },
        "shares_pending": len(_shares_backlog(cache)),
        # 분기 값이 공시된 시점(YYYYMMDD)과 쓰인 보고서 접수번호 — 밸류에이션 "공시 시점 기준" 계산용
        "available_from": {"quarter": {_label(p["end"]): p.get("avail") for p in q_out}},
        "reports_used": {"quarter": {_label(p["end"]): p.get("used") for p in q_out}},
        "fiscal_month": acc_mt,
        "unit": "원",
        "periods": {"quarter": [_label(p["end"]) for p in q_out], "ttm": [_label(p["end"]) for p in t_out],
                    "annual": [_label(p["end"]) for p in a_out]},
        "statements": statements,
        "order": {s: statements[s]["order"] for s in SHEET_ORDER},   # 재무제표 순서 (JSON 키 정렬과 무관)
        "partial_missing": partial,
        "metrics": metrics_out,
        "missing": missing,
        "calculated": calculated,
        "notes": [
            "분기 손익은 3개월 값, 4분기는 연간 - (1~3분기 합)",
            "분기 현금흐름은 누적 값의 차이",
            "4분기누적: 손익·현금흐름은 최근 4개 분기 합, 재무상태표·기말 현금은 분기 말 값",
            "주당이익의 4분기 값과 4분기누적은 차감·합산한 근사치",
            "ROE = 4분기누적(연도는 연간) 지배주주순이익 / (기초·기말 지배주주지분 평균)",
        ],
        "warnings": list(dict.fromkeys(warnings)),
        "fetched_at": max(fetched) if fetched else None,
        "cache_updated_at": cache.get("updated_at"),
    }


def get_financials(code, corp_code, name="", years=5):
    """조회 진입점. years: 출력 연도 수(5 또는 10). 수집은 최소 DEFAULT_YEARS."""
    years = max(1, min(MAX_YEARS, int(years)))
    cache, warnings = refresh(code, corp_code, max(DEFAULT_YEARS, years))
    out = compute(cache, years, name)
    out["warnings"] = warnings + out["warnings"]
    return out
