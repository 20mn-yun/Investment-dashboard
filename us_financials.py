"""미국 종목 재무 3표 (손익계산서·재무상태표·현금흐름표) — SEC XBRL companyfacts.

처음 조회할 때 companyfacts 를 한 번 받아 필요한 계정만 남겨 cache/us_financials/<티커>.json.gz 에 저장하고,
이후에는 저장본에서 계산한다. 조회 API 는 기다리지 않는다(get_financials_nowait: 뒤에서 받아오며 진행률 반환).
응답 구조는 kr_financials.get_financials_nowait 와 같다 (unit 만 "USD").
  - 하루 한 번(checked 날짜가 바뀌면) companyfacts 를 다시 받아 새 보고서를 반영
  - 계정 대응: 항목마다 계정 후보를 우선순위로 두고, 회계연도마다 그 연도의 네 기간을 가장 많이 채우는 후보를 쓴다
    (같으면 앞선 후보). 쓰인 계정은 응답 concepts_used 에 남긴다
  - 기간 구분: start~end 길이 80~100일 분기, 170~190일 반기누적, 260~285일 9개월누적, 350~380일 연간.
    같은 기간 값이 여러 보고서에 있으면 filed 가 가장 늦은 것
  - 기간 이름은 달력 기준 실제 종료 월 ("2025.09"). 종료일이 1~7일이면 전월로 붙인다 (52/53주 결산)
  - 4분기 = 연간 - (1~3분기 합). 현금흐름은 누적 값의 차이 (kr_financials 와 같은 규칙)
"""
import calendar
import gzip
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import date, datetime, timedelta

import requests

import concept_map

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "us_financials")
DISC_DIR = os.path.join(_BD, "cache", "us_disclosures")
SEC_USER_AGENT = "InvestmentDashboard changyun1222@gmail.com"
SEC_MIN_INTERVAL = 0.15          # sec_report 와 같은 호출 간격
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{doc}"
SEC_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/"

DEFAULT_YEARS = 10
MAX_YEARS = 10
MAX_RETRIES = 3                  # 실패 시 재시도 (간격 0.5 → 1 → 2초)
DISC_TTL = 1800                  # 공시 목록 저장본 재사용 (초)
DISC_DAYS = 365
DISC_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")
KEEP_FORMS = ("10-K", "10-Q", "20-F", "40-F")     # 이 글자로 시작하는 form 만 저장 (10-K/A, 10-KT 포함)
KEEP_UNITS = ("USD", "shares", "USD/shares", "pure")
SPLIT_CONCEPT = "us-gaap:StockholdersEquityNoteStockSplitConversionRatio1"   # 주식분할 비율 (yfinance 분할 이력의 대체)
PRICE_TTL = 86400                # 월말 종가 메모리 캐시 (초)
COVER_SHARES_WINDOW = 75         # 표지 발행주식수를 기간 말 뒤 며칠까지 그 기간 값으로 보는지
ROW_FIELDS = ["start", "end", "val", "form", "filed", "accn"]

# ===== 항목 대응 규칙 (한 곳에 모음) =====
# (표, 키, 이름, 성격, us-gaap 계정 후보[우선순위])  — 키·이름·성격·순서는 kr_financials.RULES 와 같다
#   성격: flow(기간 값) / stock(시점 값) / eps(주당이익, 단위 USD/shares)
RULES = [
    ("IS", "revenue", "매출액", "flow",
     ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
      "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"]),
    ("IS", "cogs", "매출원가", "flow",
     ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
      "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"]),
    ("IS", "gross_profit", "매출총이익", "flow", ["GrossProfit"]),
    ("IS", "sga", "판매관리비", "flow", ["OperatingExpenses"]),          # 연구개발비 포함 운영비용 합계. 없으면 매출총이익 - 영업이익
    ("IS", "operating_income", "영업이익", "flow", ["OperatingIncomeLoss"]),
    ("IS", "other_income", "기타수익", "flow",
     ["OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense", "OtherNonoperatingIncome"]),   # 순액 계정 (음수 가능)
    ("IS", "other_expense", "기타비용", "flow", ["OtherNonoperatingExpense"]),
    ("IS", "finance_income", "금융수익", "flow",
     ["InvestmentIncomeInterest", "InvestmentIncomeInterestAndDividend", "InterestAndDividendIncomeOperating",
      "InterestIncomeOther", "InvestmentIncomeNet", "InterestAndOtherIncome"]),
    ("IS", "finance_cost", "금융비용", "flow",
     ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestAndDebtExpense"]),
    ("IS", "equity_method", "지분법손익", "flow", ["IncomeLossFromEquityMethodInvestments"]),
    ("IS", "pretax_income", "법인세차감전순이익", "flow",
     ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"]),
    ("IS", "income_tax", "법인세", "flow", ["IncomeTaxExpenseBenefit"]),
    ("IS", "net_income", "당기순이익", "flow", ["ProfitLoss", "NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    ("IS", "net_income_owner", "지배주주순이익", "flow", ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    ("IS", "net_income_minority", "비지배주주순이익", "flow", ["NetIncomeLossAttributableToNoncontrollingInterest"]),
    ("IS", "eps_basic", "기본 주당이익", "eps", ["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"]),
    ("IS", "eps_diluted", "희석 주당이익", "eps", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),

    ("BS", "current_assets", "유동자산", "stock", ["AssetsCurrent"]),
    ("BS", "cash", "현금및현금성자산", "stock",
     ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
      "Cash", "CashAndDueFromBanks"]),
    ("BS", "st_fin", "단기금융상품", "stock",
     ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
      "DebtSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent", "HeldToMaturitySecuritiesCurrent"]),
    ("BS", "cur_fin_assets", "유동금융자산", "stock", []),                 # 미국 계정 없음 → 현금 + 단기금융상품
    ("BS", "lt_fin", "장기금융상품", "stock",
     ["LongTermInvestments", "MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
      "DebtSecuritiesNoncurrent", "AvailableForSaleSecuritiesNoncurrent", "HeldToMaturitySecuritiesNoncurrent",
      "OtherLongTermInvestments", "EquitySecuritiesFVNINoncurrent"]),
    ("BS", "noncur_fin_assets", "비유동금융자산", "stock", []),            # 미국 계정 없음 → 장기금융상품
    ("BS", "trade_receivables", "매출채권", "stock",
     ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsAndOtherReceivablesNetCurrent", "AccountsReceivableNet"]),
    ("BS", "inventories", "재고자산", "stock", ["InventoryNet"]),
    ("BS", "noncurrent_assets", "비유동자산", "stock", ["AssetsNoncurrent", "NoncurrentAssets"]),
    ("BS", "ppe", "유형자산", "stock",
     ["PropertyPlantAndEquipmentNet",
      "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"]),
    ("BS", "intangibles", "무형자산", "stock", ["IntangibleAssetsNetIncludingGoodwill"]),   # 없으면 영업권 + 무형자산 합산
    ("BS", "investment_property", "투자부동산", "stock", ["RealEstateInvestmentPropertyNet"]),
    ("BS", "associates", "관계기업 투자자산", "stock", ["EquityMethodInvestments"]),
    ("BS", "total_assets", "자산총계", "stock", ["Assets"]),
    ("BS", "current_liabilities", "유동부채", "stock", ["LiabilitiesCurrent"]),
    ("BS", "cur_fin_liab", "유동금융부채", "stock", ["DebtCurrent"]),       # 없으면 단기차입금 + 유동성장기부채 + 기업어음
    ("BS", "st_borrow", "단기차입금", "stock", ["ShortTermBorrowings", "ShorttermDebtAverageOutstandingAmount"]),
    ("BS", "cur_ltd", "유동성장기부채", "stock",
     ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtAndFinanceLeasesCurrent"]),
    ("BS", "cur_bonds", "기업어음", "stock", ["CommercialPaper"]),
    ("BS", "trade_payables", "매입채무", "stock",
     ["AccountsPayableCurrent", "AccountsPayableTradeCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"]),
    ("BS", "noncurrent_liabilities", "비유동부채", "stock", ["LiabilitiesNoncurrent"]),
    ("BS", "noncur_fin_liab", "비유동금융부채", "stock",
     ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndFinanceLeasesNoncurrent"]),
    ("BS", "lt_borrow", "장기차입금", "stock", ["LongTermLoansPayable", "LongTermNotesPayable", "OtherLongTermDebtNoncurrent"]),
    ("BS", "bonds", "사채", "stock", ["SeniorLongTermNotes", "ConvertibleLongTermNotesPayable", "ConvertibleNotesPayable"]),
    ("BS", "total_liabilities", "부채총계", "stock", ["Liabilities"]),      # 없으면 부채와자본총계 - 자본총계
    ("BS", "equity_owner", "지배주주지분", "stock", ["StockholdersEquity"]),
    ("BS", "retained_earnings", "이익잉여금", "stock", ["RetainedEarningsAccumulatedDeficit"]),
    ("BS", "equity_minority", "비지배주주지분", "stock", ["MinorityInterest"]),
    ("BS", "total_equity", "자본총계", "stock",
     ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"]),

    ("CF", "cfo", "영업현금흐름", "flow",
     ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    ("CF", "depreciation", "유형자산 감가상각비", "flow",
     ["Depreciation", "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
      "DepreciationAmortizationAndAccretionNet"]),
    ("CF", "amortization", "무형자산 상각비", "flow", ["AmortizationOfIntangibleAssets"]),
    ("CF", "cfi", "투자현금흐름", "flow",
     ["NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"]),
    ("CF", "capex_ppe", "유형자산 취득", "flow",
     ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
      "PaymentsToAcquireOtherPropertyPlantAndEquipment"]),
    ("CF", "capex_intangible", "무형자산 취득", "flow", ["PaymentsToAcquireIntangibleAssets"]),
    ("CF", "cff", "재무현금흐름", "flow",
     ["NetCashProvidedByUsedInFinancingActivities", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"]),
    ("CF", "dividends_paid", "배당금 지급", "flow", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
    ("CF", "treasury_purchase", "자기주식 취득", "flow", ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"]),
    ("CF", "cash_change", "현금 증감", "flow",
     ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
      "CashAndCashEquivalentsPeriodIncreaseDecrease",
      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
      "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect"]),
    ("CF", "cash_end", "기말 현금", "stock",
     ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalentsAtCarryingValue",
      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations"]),
]
# 표에 내보내지 않는 보조 항목 (대체 계산용)
HIDDEN_KEYS = {"st_borrow", "cur_ltd", "cur_bonds", "lt_borrow", "bonds", "goodwill", "intang_ex_gw", "liab_and_equity",
               "ppe_gross", "ppe_accum_dep"}
# 보조 계정 (표, 키, 성격, 후보) — 대체 계산에만 쓴다
AUX_RULES = [
    ("BS", "goodwill", "stock", ["Goodwill"]),
    ("BS", "intang_ex_gw", "stock", ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"]),
    ("BS", "liab_and_equity", "stock", ["LiabilitiesAndStockholdersEquity"]),
    ("BS", "ppe_gross", "stock", ["PropertyPlantAndEquipmentGross"]),
    ("BS", "ppe_accum_dep", "stock", ["AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment"]),
]
PPE_FORMULA = "유형자산 총액 - 감가상각누계액 (유형자산 순액 항목 없음)"
FIN_FALLBACK_FORMULAS = {
    "cur_fin_assets": "현금및현금성자산 + 단기금융상품 (유동금융자산 항목 없음)",
    "noncur_fin_assets": "장기금융상품 (비유동금융자산 항목 없음)",
    "cur_fin_liab": "단기차입금 + 유동성장기부채 + 기업어음 (유동금융부채 항목 없음)",
    "noncur_fin_liab": "장기차입금 + 사채 (비유동금융부채 항목 없음)",
}
INTANGIBLES_FORMULA = "영업권 + 무형자산(영업권 제외) 합산 (영업권 포함 무형자산 항목 없음)"
TOTAL_LIAB_FORMULA = "부채와자본총계 - 자본총계 (부채총계 항목 없음)"
NONCUR_ASSETS_FORMULA = "자산총계 - 유동자산 (비유동자산 항목 없음)"
NONCUR_LIAB_FORMULA = "부채총계 - 유동부채 (비유동부채 항목 없음)"
DEPRECIATION_SPLIT_FORMULA = "감가상각비 합산 계정 - 무형자산 상각비 (유형자산 감가상각비 단독 계정 없음)"
NET_INCOME_FORMULA = "지배주주순이익 + 비지배주주순이익 (당기순이익 항목 없음)"
CASH_CHANGE_FORMULA = "기말 현금 - 직전 회계연도 말 현금 (현금 증감 항목 없음)"
COMBINED_DEPRECIATION = {"DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet"}

# 직접 나오지 않으면 계산으로 채우는 항목 (kr_financials.DERIVED 와 같음)
DERIVED = [
    ("IS", "gross_profit", "매출총이익", "매출액 - 매출원가", False),
    ("IS", "sga", "판매관리비", "매출총이익 - 영업이익", False),
    ("CF", "capex", "CAPEX", "유형자산 취득 + 무형자산 취득 (지출 크기, 양수)", True),
    ("CF", "fcf", "FCF", "영업현금흐름 - CAPEX", True),
    ("BS", "fin_assets_total", "금융자산 합계", "현금및현금성자산 + 단기금융상품 + 장기금융상품 (있는 항목의 합)", True),
    ("BS", "fin_liab_total", "금융부채 합계", "유동금융부채 + 비유동금융부채 (있는 항목의 합)", True),
    ("BS", "net_fin_assets", "순금융자산", "금융자산 합계 - 금융부채 합계 (금융부채 없으면 금융자산 합계)", True),
    ("BS", "nwc", "순운전자본", "매출채권 + 재고자산 - 매입채무", True),
]
OWNER_FALLBACK = {
    "net_income_owner": ("net_income", "net_income_minority", "지배/비지배 구분 없음: 당기순이익"),
    "equity_owner": ("total_equity", "equity_minority", "지배/비지배 구분 없음: 자본총계"),
}
SHARES_CONCEPTS = {"bs": "us-gaap:CommonStockSharesOutstanding", "cover": "dei:EntityCommonStockSharesOutstanding"}

SHEET_ORDER = ["IS", "BS", "CF"]
SHEET_NAMES = {"IS": "손익계산서", "BS": "재무상태표", "CF": "현금흐름표"}
SLOTS = ["Q1", "H1", "Q3", "FY"]          # 회계연도 안의 네 보고 시점 (kr_financials 와 같은 이름)
SLOT_BACK = {"Q1": 9, "H1": 6, "Q3": 3, "FY": 0}   # 회계연도 말에서 몇 달 전에 끝나는지
# 기간 길이(일) → 종류: q 3개월, h 반기누적, n 9개월누적, y 연간
DURATION_KINDS = [(80, 100, "q"), (170, 190, "h"), (260, 285, "n"), (350, 380, "y")]


def _concepts_to_keep():
    out = set()
    for _, _, _, _, cands in RULES:
        out.update("us-gaap:" + c for c in cands)
    for _, _, _, cands in AUX_RULES:
        out.update("us-gaap:" + c for c in cands)
    out.update(SHARES_CONCEPTS.values())
    out.add(SPLIT_CONCEPT)
    return out


KEEP_CONCEPTS = _concepts_to_keep()
# 저장할 계정 목록이 바뀌면(규칙표 수정) 저장본을 다시 받도록 서명을 함께 저장한다
KEEP_SIG = hashlib.sha1(("all-concepts-v2\n" + "\n".join(sorted(KEEP_CONCEPTS))).encode()).hexdigest()[:12]


class TransientError(Exception):
    """호출 한도 초과·점검·통신 오류 — 저장하지 않는다."""


class NoDataError(Exception):
    """SEC 에 XBRL 자료가 없는 종목 (404)."""


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
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = requests.Session()
    return s


def _sec_get(url, timeout=60):
    """SEC 호출 (0.15초 간격 + 재시도). 404 는 NoDataError, 그 밖의 실패는 TransientError."""
    delay = 0.5
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        with _api_lock:
            wait = SEC_MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            res = _session().get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
            if res.status_code == 200:
                return res
            if res.status_code == 404:
                raise NoDataError(f"SEC 자료 없음 (404): {url.rsplit('/', 1)[-1]}")
            last = f"HTTP {res.status_code}"
        except NoDataError:
            raise
        except Exception as e:
            last = type(e).__name__
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    raise TransientError(f"{url.rsplit('/', 1)[-1]}: {last}")


def _atomic_write(path, data):
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


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _norm_code(code):
    return (code or "").strip().upper().replace("-", ".").replace("/", ".")


def _cache_path(code):
    return os.path.join(CACHE_DIR, f"{_norm_code(code)}.json.gz")


def _load_cache(code):
    try:
        with gzip.open(_cache_path(code), "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, EOFError, json.JSONDecodeError):
        return None


def _month_end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def _add_months(y, m, delta):
    t = y * 12 + (m - 1) + delta
    return t // 12, t % 12 + 1


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _label(d):
    return f"{d.year}.{d.month:02d}"


def _snap(d):
    """기간 종료일 → 그 기간이 속한 달의 말일 (1~7일이면 전월: 52/53주 결산)"""
    y, m = (d.year, d.month) if d.day > 7 else _add_months(d.year, d.month, -1)
    return _month_end(y, m)


def _d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ===== CIK =====
def resolve_cik(code):
    """티커 → (10자리 CIK, SEC 회사명). sec_report 의 company_tickers 저장본을 쓴다. 없으면 (None, None)"""
    sec_ticker = _norm_code(code).replace(".", "-")
    try:
        import sec_report
        return sec_report.get_cik(sec_ticker)
    except Exception as e:
        print(f"[us_financials] CIK 조회 실패 {code}: {type(e).__name__}", flush=True)
        try:   # sec_report 를 못 불러도 저장본은 읽는다
            with open(os.path.join(_BD, "cache", "sec_cik_map.json"), "r", encoding="utf-8") as f:
                entry = (json.load(f).get("map") or {}).get(sec_ticker)
            if entry:
                return f"{int(entry['cik']):010d}", entry.get("title", "")
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
    return None, None


# ===== 받아오기 =====
def _prune(data):
    """companyfacts → {"us-gaap:Revenues": {"u": 단위, "r": [[start, end, val, form, filed, accn], ...]}}
    모든 계정을 남긴다(AI·사용자가 어떤 계정을 지정해도 재수신 없이 다시 계산할 수 있게). 단위는 KEEP_UNITS 중 첫 것, form 은 KEEP_FORMS."""
    out = {}
    for tax, concepts in (data.get("facts") or {}).items():
        for name, node in concepts.items():
            cid = f"{tax}:{name}"
            units = node.get("units") or {}
            unit = next((u for u in KEEP_UNITS if u in units), None)
            if unit is None:
                continue
            rows = []
            for it in units[unit]:
                form = it.get("form") or ""
                if not form.startswith(KEEP_FORMS) or it.get("val") is None or not it.get("end"):
                    continue
                v = it["val"]
                if isinstance(v, float) and v.is_integer() and unit != "USD/shares":
                    v = int(v)
                rows.append([it.get("start"), it["end"], v, form, it.get("filed") or "", it.get("accn") or ""])
            if rows:
                out[cid] = {"u": unit, "r": rows}
    return out


def _fetch_sic(cik10):
    """SEC submissions 의 업종(SIC 코드·설명). 실패하면 (None, None) — 업종은 AI 지시문에만 쓰여 없어도 된다."""
    try:
        d = _sec_get(SEC_SUBMISSIONS_URL.format(cik=cik10), timeout=30).json()
        return (str(d.get("sic") or "") or None), (d.get("sicDescription") or None)
    except Exception as e:
        print(f"[us_financials] 업종 조회 실패 {cik10}: {type(e).__name__}", flush=True)
        return None, None


def refresh(code, cik10, entity="", progress=None):
    """companyfacts 를 받아 저장. progress(끝난 수, 전체 수). 반환: cache. 실패는 TransientError / NoDataError"""
    code = _norm_code(code)
    with _code_lock(code):
        if progress:
            progress(0, 2)
        res = _sec_get(SEC_COMPANYFACTS_URL.format(cik=cik10))
        if progress:
            progress(1, 2)
        data = res.json()
        facts = _prune(data)
        splits, splits_source = fetch_splits(code, facts)
        sic, sic_desc = _fetch_sic(cik10)
        cache = {
            "code": code, "cik": cik10, "entity": data.get("entityName") or entity,
            "sic": sic, "sic_desc": sic_desc,
            "facts": facts,
            "splits": splits, "splits_source": splits_source,
            "keep_sig": KEEP_SIG,
            "raw_concepts": sum(len(v) for v in (data.get("facts") or {}).values()),
            "fetched_at": _now(), "checked": date.today().isoformat(), "updated_at": _now(),
        }
        _atomic_write(_cache_path(code), cache)
        if progress:
            progress(2, 2)
    return cache


def needs_fetch(code):
    """저장본만 보고 (하루 한 번) 다시 받을 때가 됐는지"""
    cache = _load_cache(code)
    if not cache or not cache.get("facts"):
        return True
    if cache.get("keep_sig") != KEEP_SIG:
        return True                          # 규칙표(저장 계정)가 바뀜 → 다시 받아 새 계정을 채운다
    return (cache.get("checked") or "") < date.today().isoformat()


# ===== 뒤에서 받아오기 (조회 API 는 기다리지 않음) =====
_jobs = {}
_jobs_guard = threading.Lock()


def _run_job(code, cik10, entity, job):
    def prog(done, total):
        job["done"], job["total"] = done, total
    try:
        refresh(code, cik10, entity, progress=prog)
        try:   # 규칙이 못 잡은 핵심 항목이 있으면 AI 대응 (조건을 만족할 때만 호출)
            job["ai"] = ai_fill(code)
        except Exception as e:
            print(f"[us_financials] {code} AI 대응 에러: {type(e).__name__}: {e}", flush=True)
        job["state"] = "done"
    except (TransientError, NoDataError) as e:
        job["state"], job["error"] = "failed", str(e)
    except Exception as e:
        job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
    job["finished_at"] = _now()


def start_fetch(code, cik10=None, entity=""):
    """받아오기 작업을 뒤에서 시작(이미 진행 중이면 그 작업). 반환: 작업 정보 dict"""
    code = _norm_code(code)
    with _jobs_guard:
        job = _jobs.get(code)
        if job and job["state"] == "running":
            return job
        if not cik10:
            cik10, title = resolve_cik(code)
            entity = entity or (title or "")
        job = {"state": "running", "done": 0, "total": 2, "error": None, "warnings": [],
               "started_at": _now(), "finished_at": None}
        if not cik10:
            job.update(state="failed", error=f"SEC CIK 를 찾을 수 없음: {code}", finished_at=_now())
            job["thread"] = None
        else:
            job["thread"] = threading.Thread(target=_run_job, args=(code, cik10, entity, job), daemon=True)
        _jobs[code] = job
        if job["thread"]:
            job["thread"].start()
        return job


def fetch_blocking(code, cik10=None, entity=""):
    """미리 받기용: 작업을 시작(또는 진행 중인 작업에 합류)하고 끝날 때까지 기다린다."""
    job = start_fetch(code, cik10, entity)
    if job.get("thread"):
        job["thread"].join()
    return job


def job_progress(job):
    return {"done": job.get("done", 0), "total": job.get("total")}


def get_financials_nowait(code, cik10=None, name="", years=5):
    """저장본이 있으면 바로 계산해 돌려주고, 없으면 뒤에서 받아오며 진행률을 돌려준다.
    status: ready | fetching | failed. ready 이면서 뒤에서 갱신 중이면 updating 에 진행률. (kr_financials 와 같은 규칙)"""
    code = _norm_code(code)
    years = max(1, min(MAX_YEARS, int(years)))
    with _jobs_guard:
        job = _jobs.get(code)
        failed = None
        if job and job["state"] in ("done", "failed"):
            _jobs.pop(code, None)
            if job["state"] == "failed":
                failed = job
            job = None
    cache = _load_cache(code)
    usable = bool(cache and cache.get("facts"))
    if job:
        if usable:
            out = compute(cache, years, name)
            out.update(status="ready", updating=job_progress(job))
            return out
        return {"status": "fetching", "market": "US", "code": code, "name": name, "progress": job_progress(job)}
    if failed and not usable:
        return {"status": "failed", "market": "US", "code": code, "name": name, "error": failed["error"]}
    if not usable:
        job = start_fetch(code, cik10, name)
        if job["state"] == "failed":
            with _jobs_guard:
                _jobs.pop(code, None)
            return {"status": "failed", "market": "US", "code": code, "name": name, "error": job["error"]}
        return {"status": "fetching", "market": "US", "code": code, "name": name, "progress": job_progress(job)}
    out = compute(cache, years, name)
    out["status"] = "ready"
    out["updating"] = None
    if failed:
        out["fetch_error"] = failed["error"]
        out["warnings"] = [failed["error"]] + out["warnings"]
    elif needs_fetch(code):
        job = start_fetch(code, cik10 or cache.get("cik"), name)
        out["updating"] = job_progress(job) if job["state"] == "running" else None
    elif _ai_needed(code, out):
        job = _start_ai(code)
        if job:
            out["updating"] = {"done": 0, "total": 1, "stage": "ai"}
    return out


# ===== 계산 =====
def _sub(a, b):
    return None if a is None or b is None else a - b


def _add(*xs):
    return None if any(x is None for x in xs) else sum(xs)


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(a / b * 100, 2)


def _growth(cur, prev):
    if cur is None or prev is None or prev <= 0:
        return None
    return round((cur - prev) / prev * 100, 2)


def _ranges(all_labels, gaps):
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


def _duration_kind(start, end):
    s, e = _d(start), _d(end)
    if not s or not e:
        return None
    days = (e - s).days
    for lo, hi, kind in DURATION_KINDS:
        if lo <= days <= hi:
            return kind
    return None


def _index_concept(node):
    """계정 하나의 행들 → {"flow": {(기간말 월말, 종류): fact}, "inst": {기간말 월말: fact}}
    fact = {"val", "filed", "accn", "end", "first_filed", "first_accn"}. 같은 기간은 filed 가 가장 늦은 값."""
    flow, inst = {}, {}
    for start, end, val, form, filed, accn in node.get("r", []):
        e = _d(end)
        if not e:
            continue
        if start:
            kind = _duration_kind(start, end)
            if not kind:
                continue
            key, target = (_snap(e), kind), flow
        else:
            key, target = _snap(e), inst
        cur = target.get(key)
        if cur is None:
            target[key] = {"val": val, "filed": filed, "accn": accn, "end": end, "form": form,
                           "first_filed": filed, "first_accn": accn}
        else:
            if filed >= cur["filed"]:
                cur.update(val=val, filed=filed, accn=accn, end=end, form=form)
            if filed < cur["first_filed"]:
                cur.update(first_filed=filed, first_accn=accn)
    return {"flow": flow, "inst": inst}


def fiscal_month(cache):
    """연간(350~380일) 값 중 가장 최근에 끝난 기간의 월. 없으면 10-K 시점 값, 그래도 없으면 12."""
    facts = cache.get("facts") or {}
    best = None
    for _, _, _, kind, cands in RULES:
        if kind == "stock":
            continue
        for c in cands:
            node = facts.get("us-gaap:" + c)
            if not node:
                continue
            for start, end, val, form, filed, accn in node["r"]:
                if start and form.startswith(("10-K", "20-F", "40-F")) and _duration_kind(start, end) == "y":
                    e = _d(end)
                    if e and (best is None or e > best):
                        best = e
    if best is None:
        for cid, node in facts.items():
            for start, end, val, form, filed, accn in node["r"]:
                if not start and form.startswith("10-K"):
                    e = _d(end)
                    if e and (best is None or e > best):
                        best = e
    return _snap(best).month if best else 12


def _fy_end_of(d, acc_mt):
    y = d.year if d.month <= acc_mt else d.year + 1
    return _month_end(y, acc_mt)


def _slot_ends(fy_end):
    return {k: _month_end(*_add_months(fy_end.year, fy_end.month, -back)) for k, back in SLOT_BACK.items()}


def _slot_fact(idx, sheet, kind, slot, slot_end):
    """계정 인덱스에서 (슬롯) 값을 (pos0, pos1, fact) 로. IS: (3개월, 누적) / CF: (누적, None) / stock: (시점, None)"""
    if kind == "stock":
        f = idx["inst"].get(slot_end)
        return (f["val"], None, f) if f else None
    flow = idx["flow"]
    if slot == "FY":
        f = flow.get((slot_end, "y"))
        return (f["val"], f["val"], f) if f else None
    cum_kind = {"Q1": "q", "H1": "h", "Q3": "n"}[slot]
    q = flow.get((slot_end, "q"))
    c = flow.get((slot_end, cum_kind))
    if sheet == "CF":
        return (c["val"], None, c) if c else None
    if q is None and c is None:
        return None
    return (q["val"] if q else None, c["val"] if c else None, q or c)


def _same_meaning(ia, ib):
    """두 계정이 겹치는 기간에서 모두 같은 값이면 같은 뜻(회사가 계정 이름만 바꾼 경우). 겹치는 기간이 없으면 False."""
    shared = False
    for part in ("flow", "inst"):
        for k in set(ia[part]) & set(ib[part]):
            shared = True
            a, b = ia[part][k]["val"], ib[part][k]["val"]
            if a != b and abs(a - b) > max(1, abs(a) * 1e-6):
                return False
    return shared


def _cid(name):
    """계정 이름 → 저장본 키. 분류 접두어가 없으면 us-gaap"""
    name = (name or "").strip()
    return name if ":" in name else "us-gaap:" + name


def _cname(cid):
    """저장본 키 → 표시 이름 (us-gaap 접두어는 뗀다)"""
    return cid[8:] if cid.startswith("us-gaap:") else cid


class _LazyIdx(dict):
    """계정 인덱스를 처음 쓸 때 만든다 (저장본에 전체 계정이 있어 전부 인덱싱하면 느리다)"""
    def __init__(self, facts):
        super().__init__()
        self._facts = facts

    def __missing__(self, cid):
        node = self._facts.get(cid)
        if node is None:
            raise KeyError(cid)
        v = self[cid] = _index_concept(node)
        return v

    def __contains__(self, cid):
        return cid in self._facts

    def get(self, cid, default=None):
        return self[cid] if cid in self._facts else default


def _pick_concepts(facts_idx, fy_ends, overrides=None):
    """회계연도마다 항목별로 쓸 계정: 그 연도의 네 슬롯을 가장 많이 채우는 후보 (같으면 앞선 후보).
    선택한 계정에 없는 슬롯은, 겹치는 기간 값이 모두 같아 같은 뜻으로 확인된 다른 후보(동의어)로만 채운다.
    overrides(대응표의 user·ai 지정) {키: 계정 또는 None}: 지정 계정만 쓰고, None 이면 그 항목은 비운다.
    반환: {fy_end: {key: (sheet, kind, concept_id, [동의어 concept_id])}}"""
    out = {}
    overrides = overrides or {}
    all_rules = [(s, k, kind, cands) for s, k, _, kind, cands in RULES] + [(s, k, kind, cands) for s, k, kind, cands in AUX_RULES]
    syn_cache = {}

    def synonyms(cid, cands):
        if cid not in syn_cache:
            syn_cache[cid] = [c for c in cands if c != cid and c in facts_idx and _same_meaning(facts_idx[cid], facts_idx[c])]
        return syn_cache[cid]

    for fy_end in fy_ends:
        ends = _slot_ends(fy_end)
        chosen = {}
        for sheet, key, kind, cands in all_rules:
            if key in overrides:
                oc = overrides[key]
                if oc and _cid(oc) in facts_idx:
                    chosen[key] = (sheet, kind, _cid(oc), [])
                continue
            cids = ["us-gaap:" + c for c in cands if "us-gaap:" + c in facts_idx]
            best, best_n = None, 0
            for cid in cids:
                n = sum(1 for slot, se in ends.items() if _slot_fact(facts_idx[cid], sheet, kind, slot, se) is not None)
                if n > best_n:
                    best, best_n = cid, n
            if best:
                chosen[key] = (sheet, kind, best, synonyms(best, cids))
        out[fy_end] = chosen
    return out


def _extract_fy(facts_idx, chosen, fy_end, prev_fy_end):
    """회계연도 하나 → {슬롯: {"v": {키: (pos0, pos1)}, "calc": {키: 계산식}, "used": {키: 계정}, "fact": {키: fact}}}"""
    ends = _slot_ends(fy_end)
    reps = {}
    for slot, se in ends.items():
        v, calc, used, facts = {}, {}, {}, {}
        for key, (sheet, kind, cid, alts) in chosen.items():
            for c in [cid] + alts:
                r = _slot_fact(facts_idx[c], sheet, kind, slot, se)
                if r is not None:
                    v[key] = (r[0], r[1])
                    used[key] = c
                    facts[key] = r[2]
                    break

        def _stock_sum(keys):
            xs = [v[k][0] for k in keys if k in v and v[k][0] is not None]
            return sum(xs) if xs else None

        # 금융자산·금융부채 대체 (합계 항목이 없을 때)
        for key, parts in (("cur_fin_assets", ["cash", "st_fin"]), ("noncur_fin_assets", ["lt_fin"]),
                           ("cur_fin_liab", ["st_borrow", "cur_ltd", "cur_bonds"]), ("noncur_fin_liab", ["lt_borrow", "bonds"])):
            if key not in v:
                s = _stock_sum(parts)
                if s is not None:
                    v[key] = (s, None)
                    calc[key] = FIN_FALLBACK_FORMULAS[key]
        # 무형자산(영업권 포함) 이 없으면 영업권 + 무형자산 합산
        if "intangibles" not in v:
            s = _stock_sum(["goodwill", "intang_ex_gw"])
            if s is not None:
                v["intangibles"] = (s, None)
                calc["intangibles"] = INTANGIBLES_FORMULA
        # 유형자산 순액이 없으면 총액 - 감가상각누계액
        if "ppe" not in v and "ppe_gross" in v and "ppe_accum_dep" in v:
            s = _sub(v["ppe_gross"][0], abs(v["ppe_accum_dep"][0]))
            if s is not None:
                v["ppe"] = (s, None)
                calc["ppe"] = PPE_FORMULA
        # 부채총계 = 부채와자본총계 - 자본총계
        if "total_liabilities" not in v and "liab_and_equity" in v and "total_equity" in v:
            s = _sub(v["liab_and_equity"][0], v["total_equity"][0])
            if s is not None:
                v["total_liabilities"] = (s, None)
                calc["total_liabilities"] = TOTAL_LIAB_FORMULA
        # 비유동자산·비유동부채 = 총계 - 유동
        if "noncurrent_assets" not in v and "total_assets" in v and "current_assets" in v:
            s = _sub(v["total_assets"][0], v["current_assets"][0])
            if s is not None:
                v["noncurrent_assets"] = (s, None)
                calc["noncurrent_assets"] = NONCUR_ASSETS_FORMULA
        if "noncurrent_liabilities" not in v and "total_liabilities" in v and "current_liabilities" in v:
            s = _sub(v["total_liabilities"][0], v["current_liabilities"][0])
            if s is not None:
                v["noncurrent_liabilities"] = (s, None)
                calc["noncurrent_liabilities"] = NONCUR_LIAB_FORMULA
        # 감가상각비가 합산 계정이고 무형자산 상각비가 따로 있으면 차감해 유형자산 감가상각비로
        if "depreciation" in v and used.get("depreciation", "").split(":")[-1] in COMBINED_DEPRECIATION and "amortization" in v:
            dp, am = v["depreciation"], v["amortization"]
            v["depreciation"] = (_sub(dp[0], am[0]), _sub(dp[1], am[1]))
            if v["depreciation"] == (None, None):
                v["depreciation"] = dp
            else:
                calc["depreciation"] = DEPRECIATION_SPLIT_FORMULA
        # 당기순이익: ProfitLoss 가 없고 비지배가 있으면 지배 + 비지배
        if used.get("net_income", "").split(":")[-1] != "ProfitLoss" and "net_income_owner" in v and "net_income_minority" in v:
            o, m = v["net_income_owner"], v["net_income_minority"]
            s = (_add(o[0], m[0]), _add(o[1], m[1]))
            if s != (None, None):
                v["net_income"] = s
                calc["net_income"] = NET_INCOME_FORMULA
        reps[slot] = {"v": v, "calc": calc, "used": used, "fact": facts, "end": se}
    # 현금 증감이 없으면 기말 현금 - 직전 회계연도 말 현금 (누적)
    prev_cash = None
    if prev_fy_end is not None:
        pc = chosen.get("cash_end")
        if pc:
            for c in [pc[2]] + pc[3]:
                r = _slot_fact(facts_idx[c], "CF", "stock", "FY", prev_fy_end)
                if r:
                    prev_cash = r[0]
                    break
    for slot, rep in reps.items():
        v = rep["v"]
        if "cash_change" not in v and v.get("cash_end", (None,))[0] is not None and prev_cash is not None:
            v["cash_change"] = (v["cash_end"][0] - prev_cash, None)
            rep["calc"]["cash_change"] = CASH_CHANGE_FORMULA
    return reps


def _group_same_date(rows):
    """주식수 시점 값: 같은 보고서(accn)에 같은 날짜 값이 여러 개면 종류별 주식수 → 합산. 날짜별로 filed 최신 보고서를 쓴다.
    반환: {end(date): {"val", "filed", "accn"}}"""
    by_date = {}
    for start, end, val, form, filed, accn in rows:
        if start:
            continue
        e = _d(end)
        if not e:
            continue
        g = by_date.setdefault(e, {})
        a = g.setdefault(accn, {"vals": set(), "filed": filed})
        a["vals"].add(val)
    out = {}
    for e, g in by_date.items():
        accn, a = max(g.items(), key=lambda kv: kv[1]["filed"])
        out[e] = {"val": sum(a["vals"]), "filed": a["filed"], "accn": accn, "classes": len(a["vals"])}
    return out


def _shares_index(facts):
    bs = _group_same_date((facts.get(SHARES_CONCEPTS["bs"]) or {}).get("r", []))
    cover = _group_same_date((facts.get(SHARES_CONCEPTS["cover"]) or {}).get("r", []))
    return bs, cover


# ===== 주식분할 =====
# 분할 전에 제출된 보고서의 주식수·주당 값은 분할 전 기준이고, 분할 뒤 제출된 보고서는 과거 비교 값까지 분할 후 기준으로
# 재작성돼 있다. 그래서 값마다 "제출일(filed) 이후에 있었던 분할" 비율만 곱해(주식수) 또는 나눠(EPS) 현재 기준으로 맞춘다.
def _yahoo_symbol(code):
    try:
        import watchlist as watchlist_store
        return watchlist_store.to_source_ticker(code, "yahoo")
    except Exception:
        return _norm_code(code).replace(".", "-")


def _fetch_splits_yf(code):
    """yfinance 분할 이력 → [[날짜, 비율], ...] (오래된 것부터). 실패하면 None"""
    try:
        import yfinance as yf
        s = yf.Ticker(_yahoo_symbol(code)).splits
        out = []
        for ts, ratio in s.items():
            r = float(ratio)
            if r > 0 and abs(r - 1) > 1e-6:
                out.append([ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10], r])
        return sorted(out)
    except Exception as e:
        print(f"[us_financials] {code} yfinance 분할 이력 실패: {type(e).__name__}", flush=True)
        return None


def _splits_from_sec(facts):
    """companyfacts 의 분할 비율 계정 → [[날짜, 비율]]. 같은 비율이 120일 안에 반복되면 하나(가장 이른 날짜)로 본다."""
    node = facts.get(SPLIT_CONCEPT)
    if not node:
        return []
    pts = sorted({(end, float(val)) for start, end, val, form, filed, accn in node["r"] if val and float(val) > 0 and float(val) != 1})
    out = []
    for end, ratio in pts:
        if out and out[-1][1] == ratio and (_d(end) - _d(out[-1][0])).days <= 120:
            continue
        out.append([end, ratio])
    return out


def _infer_splits(cover):
    """분할 이력이 없을 때: 표지 발행주식수(제출 때마다 그 시점 값이라 재작성되지 않음)가 연속해서 1.9배 이상 정수배 근처로
    뛰면 분할로 본다. 날짜는 분할 뒤 첫 표지 날짜. 반환 [[날짜, 비율]]"""
    series = sorted({(e, x["val"]) for e, x in cover.items() if x["val"]})
    out = []
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        if not v0 or not v1:
            continue
        r = v1 / v0
        if r >= 1.9:
            n = round(r)
            if abs(r - n) <= 0.1 * n:
                out.append([d1.isoformat(), float(n)])
        elif r <= 1 / 1.9:
            n = round(1 / r)
            if abs(1 / r - n) <= 0.1 * n:
                out.append([d1.isoformat(), 1.0 / n])
    return out


def fetch_splits(code, facts):
    """분할 이력: yfinance → SEC 분할 비율 계정 → 없음. 반환: ([[날짜, 비율]], 출처)"""
    s = _fetch_splits_yf(code)
    if s is not None:
        return s, "yfinance"
    s = _splits_from_sec(facts)
    return s, ("sec" if s else None)


def _split_factor(splits, when):
    """날짜 when(ISO) 이후에 있었던 분할 비율의 곱"""
    f = 1.0
    for d, r in splits:
        if d > when:
            f *= r
    return f


def _apply_splits(facts, splits, by="filed"):
    """주식수(shares)는 비율을 곱하고 주당 값(USD/shares)은 나눠 현재 기준으로. by: filed(제출일) | end(기간 말).
    반환: (조정된 facts 복사본, 조정한 값 수)"""
    if not splits:
        return facts, 0
    out, n = {}, 0
    for cid, node in facts.items():
        u = node.get("u")
        if u not in ("shares", "USD/shares"):
            out[cid] = node
            continue
        rows = []
        for start, end, val, form, filed, accn in node["r"]:
            f = _split_factor(splits, (filed if by == "filed" and filed else end))
            if f != 1.0 and val is not None:
                val = val * f if u == "shares" else val / f
                if u == "shares":
                    val = int(round(val))
                else:
                    val = round(val, 4)
                n += 1
            rows.append([start, end, val, form, filed, accn])
        out[cid] = {"u": u, "r": rows}
    return out, n


def adjusted_facts(cache):
    """저장본 → (분할 조정된 facts, 분할 이력, 출처, 조정한 값 수). 이력이 없으면 주식수 점프로 추정(end 기준)"""
    facts = cache.get("facts") or {}
    splits = cache.get("splits") or []
    source = cache.get("splits_source")
    by = "filed"
    if not splits:
        splits = _splits_from_sec(facts)
        source = "sec" if splits else None
    if not splits:
        bs, cover = _shares_index(facts)
        splits = _infer_splits(cover)
        source = "inferred" if splits else None
    adj, n = _apply_splits(facts, splits, by)
    return adj, splits, source, n


def _shares_for(bs, cover, slot_end, actual_end=None):
    """기간 말 발행주식수. 재무상태표 시점 값(source=sec) → 표지 값(기간 말 뒤 75일 이내, source=cover) → 직전 값 이월(carry)."""
    def _info(dt, v, source, src_dt=None):
        return {"common": v, "total": v, "issued": None, "treasury": None,
                "stlm_dt": dt.isoformat(), "source": source, "from": (src_dt or dt).isoformat()}

    cands = [e for e in bs if _snap(e) == slot_end]
    if cands:
        e = max(cands)
        return _info(e, bs[e]["val"], "sec")
    base = _d(actual_end) or slot_end
    win = [e for e in cover if base < e <= base + timedelta(days=COVER_SHARES_WINDOW)]
    if win:
        e = min(win)
        return _info(e, cover[e]["val"], "cover")
    prev = [(e, x["val"]) for e, x in list(bs.items()) + list(cover.items()) if e <= slot_end]
    if not prev:
        return None
    e, v = max(prev, key=lambda t: t[0])
    return _info(e, v, "carry", e)


def latest_shares(cache):
    """가장 최근 발행주식수 (분할 조정, 표지 값 우선). 반환: (주식수, 날짜) 또는 (None, None)"""
    bs, cover = _shares_index(adjusted_facts(cache)[0])
    allv = list(bs.items()) + list(cover.items())
    if not allv:
        return None, None
    e, x = max(allv, key=lambda kv: kv[0])
    return x["val"], e.isoformat()


def compute(cache, years=5, name="", persist=True):
    """저장본 → 응답. persist=True 면 규칙이 고른 계정을 대응표(rule 출처)에 스냅샷으로 남긴다."""
    facts, splits, splits_source, n_adjusted = adjusted_facts(cache)
    facts_idx = _LazyIdx(facts)
    overrides = concept_map.overrides("US", cache["code"]) if persist else {}
    cmap_items = concept_map.load("US", cache["code"])["items"] if persist else {}
    acc_mt = fiscal_month(cache)
    today = date.today()
    fy_last = _fy_end_of(today, acc_mt)
    warnings = []
    kinds = {key: (sheet, kind) for sheet, key, label, kind, cands in RULES}

    n_fy = MAX_YEARS + 1
    fy_ends = [_month_end(fy_last.year - i, acc_mt) for i in range(n_fy - 1, -1, -1)]
    chosen_by_fy = _pick_concepts(facts_idx, fy_ends, overrides)
    fys = []
    prev_end = _month_end(fy_ends[0].year - 1, acc_mt)
    for fy_end in fy_ends:
        reps = _extract_fy(facts_idx, chosen_by_fy[fy_end], fy_end, prev_end)
        fys.append({"fy_end": fy_end, "reps": reps, "chosen": chosen_by_fy[fy_end]})
        prev_end = fy_end

    # 분기 값 (kr_financials.compute 와 같은 규칙: 손익은 3개월 값, 4분기 = 연간 - 1~3분기, 현금흐름은 누적 차이)
    quarters, annual = [], []
    for fy in fys:
        reps = fy["reps"]
        g = lambda k, key: reps[k]["v"].get(key)
        qv = {k: {} for k in SLOTS}
        for key, (sheet, kind) in kinds.items():
            r1, r2, r3, r4 = g("Q1", key), g("H1", key), g("Q3", key), g("FY", key)
            a = lambda r: r[0] if r else None
            ad = lambda r: r[1] if r else None
            if kind == "stock":
                q = [a(r1), a(r2), a(r3), a(r4)]
            elif sheet == "CF":
                c = [a(r1), a(r2), a(r3), a(r4)]
                q = [c[0], _sub(c[1], c[0]), _sub(c[2], c[1]), _sub(c[3], c[2])]
            else:
                q1 = a(r1) if a(r1) is not None else ad(r1)
                q2 = a(r2)
                q3 = a(r3)
                c2 = ad(r2) if ad(r2) is not None else _add(q1, q2)
                if q2 is None:
                    q2 = _sub(ad(r2), q1)
                if q3 is None:
                    q3 = _sub(ad(r3), c2)
                c3 = ad(r3) if ad(r3) is not None else _add(c2, q3)
                q123 = _add(q1, q2, q3)
                q4 = _sub(a(r4), q123 if q123 is not None else c3)
                q = [q1, q2, q3, q4]
                if kind == "eps":
                    q = [None if x is None else round(x, 4) for x in q]
            for k, v in zip(SLOTS, q):
                if v is not None:
                    qv[k][key] = v
        for idx, k in enumerate(SLOTS):
            rcalc, used, filed = {}, [], []
            for kk in SLOTS[:idx + 1]:
                rep = reps.get(kk)
                if rep and rep["v"]:
                    rcalc.update(rep["calc"])
                    ff = [(f["first_filed"], f["first_accn"]) for f in rep["fact"].values() if f.get("first_filed")]
                    if ff:
                        fl, ac = min(ff)
                        filed.append(fl)
                        used.append(ac)
            rep = reps[k]
            quarters.append({"end": rep["end"], "fs": "CFS" if rep["v"] else None, "vals": qv[k], "rcalc": rcalc,
                             "used": used, "avail": max(filed).replace("-", "") if filed else None,
                             "concepts": rep["used"]})
        fy_rep = reps["FY"]
        annual.append({"end": fy["fy_end"], "fs": "CFS" if fy_rep["v"] else None,
                       "vals": {key: fy_rep["v"][key][0] for key in kinds if key in fy_rep["v"] and fy_rep["v"][key][0] is not None},
                       "rcalc": dict(fy_rep["calc"]), "concepts": fy_rep["used"]})

    # 4분기누적
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
                    vals[key] = round(sum(xs), 4) if kind == "eps" else sum(xs)
        rcalc = {}
        for x in (last4 or [q]):
            rcalc.update(x["rcalc"])
        ttm.append({"end": q["end"], "fs": q["fs"], "vals": vals, "rcalc": rcalc})

    # 계산 항목 (kr_financials 와 같음)
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
        for p in series:
            v = p["vals"]
            m = {
                "operating_margin": _pct(v.get("operating_income"), v.get("revenue")),
                "net_margin": _pct(v.get("net_income"), v.get("revenue")),
                "debt_ratio": _pct(v.get("total_liabilities"), v.get("total_equity")),
            }
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

    mq, mt, ma = metrics(quarters, "quarter"), metrics(ttm, "ttm"), metrics(annual, "annual")
    metrics_out = {
        "quarter": {_label(p["end"]): mq[_label(p["end"])] for p in q_out},
        "ttm": {_label(p["end"]): mt[_label(p["end"])] for p in t_out},
        "annual": {_label(p["end"]): ma[_label(p["end"])] for p in a_out},
    }

    # 표
    labels = {key: label for sheet, key, label, kind, cands in RULES}
    labels.update({key: label for sheet, key, label, f, a in DERIVED})
    sheet_keys = {s: [] for s in SHEET_ORDER}
    for sheet, key, label, kind, cands in RULES:
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

    statements, missing, partial = {}, [], []
    for s in SHEET_ORDER:
        items = {}
        for key in sheet_keys[s]:
            q = {_label(p["end"]): p["vals"].get(key) for p in q_out}
            t = {_label(p["end"]): p["vals"].get(key) for p in t_out}
            a = {_label(p["end"]): p["vals"].get(key) for p in a_out}
            allv = list(q.values()) + list(t.values()) + list(a.values())
            if all(x is None for x in allv):
                missing.append({"sheet": s, "key": key, "label": labels[key]})
            items[key] = {"label": labels[key], "quarter": q, "ttm": t, "annual": a}
            if not all(x is None for x in allv):
                for view, ser in (("quarter", q), ("ttm", t), ("annual", a)):
                    gaps = [lb for lb, x in ser.items() if x is None]
                    if gaps:
                        partial.append({"sheet": s, "key": key, "label": labels[key], "view": view,
                                        "periods": gaps, "ranges": _ranges(list(ser), gaps)})
        statements[s] = {"name": SHEET_NAMES[s], "items": items, "order": list(sheet_keys[s])}

    keep = {"quarter": {_label(p["end"]) for p in q_out}, "ttm": {_label(p["end"]) for p in t_out},
            "annual": {_label(p["end"]) for p in a_out}}
    for c in calculated.values():
        for view in c["periods"]:
            c["periods"][view] = [lb for lb in c["periods"][view] if lb in keep[view]]
    calculated = {k: v for k, v in calculated.items() if any(v["periods"].values())}

    # 쓰인 계정 (항목별: 최근 회계연도 계정, 연도별 계정, 출처 rule|ai|user 와 근거·확신도)
    concepts_used = {key: {"concept": None, "by_period": {}, "concepts": [], "source": "rule", "confidence": None, "reason": None}
                     for sheet, key, label, kind, cands in RULES if key not in HIDDEN_KEYS}
    for p in a_out + q_out:
        for key, cid in p.get("concepts", {}).items():
            if key in HIDDEN_KEYS:
                continue
            concepts_used[key]["by_period"][_label(p["end"])] = _cname(cid)
    rule_snapshot = {}
    for key, c in concepts_used.items():
        last = max(c["by_period"]) if c["by_period"] else None
        c["concept"] = c["by_period"].get(last)
        c["concepts"] = sorted(set(c["by_period"].values()))
        m = cmap_items.get(key)
        if m and m.get("source") in ("ai", "user"):
            c.update(source=m["source"], confidence=m.get("confidence"), reason=m.get("reason"),
                     mapped=(_cname(_cid(m["concept"])) if m.get("concept") else None), decided_at=m.get("decided_at"),
                     has_prev=bool(m.get("prev")))
        else:
            rule_snapshot[key] = c["concept"]
    if persist:
        try:
            concept_map.set_rules("US", cache["code"], rule_snapshot, KEEP_SIG)
        except OSError as e:
            print(f"[us_financials] 대응표 저장 실패 {cache['code']}: {type(e).__name__}", flush=True)
    ai_n = sum(1 for c in concepts_used.values() if c["source"] == "ai")
    ai_low = sum(1 for c in concepts_used.values() if c["source"] == "ai" and c.get("confidence") == "low")
    user_n = sum(1 for c in concepts_used.values() if c["source"] == "user")

    # 주식수 (기간의 실제 종료일 = 그 슬롯에 쓰인 fact 의 end)
    bs_sh, cover_sh = _shares_index(facts)
    # 분할 조정 표시: 마지막 분할일보다 먼저 끝난 기간은 주식수·EPS 가 현재(분할 후) 기준으로 환산된 값
    last_split = max((d for d, r in splits), default=None)
    split_adj = {"splits": [{"date": d, "ratio": r} for d, r in splits], "source": splits_source, "adjusted_values": n_adjusted,
                 "shares_periods": {"quarter": [], "annual": []}, "eps_periods": {"quarter": [], "ttm": [], "annual": []}}
    if last_split:
        for view, ser in (("quarter", q_out), ("annual", a_out)):
            split_adj["shares_periods"][view] = [_label(p["end"]) for p in ser if p["end"].isoformat() < last_split]
        for view, ser in (("quarter", q_out), ("ttm", t_out), ("annual", a_out)):
            split_adj["eps_periods"][view] = [_label(p["end"]) for p in ser if p["end"].isoformat() < last_split
                                              and (p["vals"].get("eps_basic") is not None or p["vals"].get("eps_diluted") is not None)]
    actual_end = {}
    for fy in fys:
        for rep in fy["reps"].values():
            f = rep["fact"].get("total_assets") or next(iter(rep["fact"].values()), None)
            if f:
                actual_end[rep["end"]] = f["end"]

    def shares(p):
        return _shares_for(bs_sh, cover_sh, p["end"], actual_end.get(p["end"]))

    # 10-K 에 결산월과 다른 달에 끝난 연간 값이 있으면 경고 (결산월 변경). 10-Q 의 최근 12개월 값(아마존 등)은 조용히 뺀다
    off = set()
    for cid, idx in facts_idx.items():
        for (se, kind), f in idx["flow"].items():
            if kind == "y" and se.month != acc_mt and _d(f["end"]) and _d(f["end"]) >= fy_ends[0] and f.get("form", "").startswith("10-K"):
                off.add(_label(se))
    if off:
        warnings.append(f"결산월({acc_mt}월)과 다른 달에 끝난 연간 값({', '.join(sorted(off))})은 표에 넣지 않음 (결산월 변경 가능성)")

    fetched = cache.get("fetched_at")
    return {
        "market": "US",
        "code": cache["code"],
        "name": name or cache.get("entity", ""),
        "entity": cache.get("entity", ""),
        "cik": cache.get("cik"),
        "fs_div": "CFS",
        "fs_div_label": "연결",
        "fs_div_by_period": {
            "quarter": {_label(p["end"]): p["fs"] for p in q_out},
            "annual": {_label(p["end"]): p["fs"] for p in a_out},
        },
        "shares_by_period": {
            "quarter": {_label(p["end"]): shares(p) for p in q_out},
            "annual": {_label(p["end"]): shares(p) for p in a_out},
        },
        "shares_pending": 0,
        "split_adjustment": split_adj,
        "available_from": {"quarter": {_label(p["end"]): p.get("avail") for p in q_out}},
        "reports_used": {"quarter": {_label(p["end"]): p.get("used") for p in q_out}},
        "concepts_used": concepts_used,
        "concept_map_summary": {"ai": ai_n, "ai_low": ai_low, "user": user_n, "sic": cache.get("sic"), "sic_desc": cache.get("sic_desc")},
        "fiscal_month": acc_mt,
        "unit": "USD",
        "periods": {"quarter": [_label(p["end"]) for p in q_out], "ttm": [_label(p["end"]) for p in t_out],
                    "annual": [_label(p["end"]) for p in a_out]},
        "statements": statements,
        "order": {s: statements[s]["order"] for s in SHEET_ORDER},
        "partial_missing": partial,
        "metrics": metrics_out,
        "missing": missing,
        "calculated": calculated,
        "notes": [
            "출처: SEC XBRL companyfacts (10-K·10-Q). 기간 이름은 실제 종료 월 (달력 기준)",
            "분기 손익은 3개월 값, 4분기는 연간 - (1~3분기 합)",
            "분기 현금흐름은 누적 값의 차이",
            "4분기누적: 손익·현금흐름은 최근 4개 분기 합, 재무상태표·기말 현금은 분기 말 값",
            "주당이익의 4분기 값과 4분기누적은 차감·합산한 근사치",
            "주식수와 주당이익은 주식분할을 반영해 현재 주식수 기준으로 환산 (split_adjustment 참고)",
            "ROE = 4분기누적(연도는 연간) 지배주주순이익 / (기초·기말 지배주주지분 평균)",
            "기타수익은 순액 계정(OtherNonoperatingIncomeExpense 등)이라 음수일 수 있음",
            "유형자산 취득·배당·자기주식 취득은 지출 크기(양수)",
        ],
        "warnings": list(dict.fromkeys(warnings)),
        "fetched_at": fetched,
        "cache_updated_at": cache.get("updated_at"),
    }


def get_financials(code, cik10=None, name="", years=5):
    """조회 진입점(기다림). years: 출력 연도 수(5 또는 10)."""
    code = _norm_code(code)
    if not cik10:
        cik10, title = resolve_cik(code)
        name = name or title or ""
    if not cik10:
        raise NoDataError(f"SEC CIK 를 찾을 수 없음: {code}")
    cache = refresh(code, cik10, name) if needs_fetch(code) else _load_cache(code)
    return compute(cache, years, name)


# ===== 종목 요약 (시가총액·PER·PBR·ROE·결산월) =====
_summary_cache = {}
_summary_lock = threading.Lock()
SUMMARY_TTL = 60


def get_summary(code, name):
    """미국 종목 요약. 현재가 등은 stock_profile.us_summary(yfinance) 를 쓰고,
    시가총액·PER·PBR·ROE·결산월은 재무 3표 저장본으로 계산한다 (저장본이 없으면 yfinance 값 그대로)."""
    import stock_profile
    code = _norm_code(code)
    base = stock_profile.get_summary("US", code, name)
    with _summary_lock:
        hit = _summary_cache.get(code)
        if hit and time.time() - hit[0] < SUMMARY_TTL:
            out = dict(base)
            out.update(hit[1])
            return out
    cache = _load_cache(code)
    if not cache or not cache.get("facts"):
        return base
    try:
        fin = compute(cache, 5, name)
    except Exception as e:
        print(f"[us_financials] {code} 재무 계산 실패: {type(e).__name__}: {e}", flush=True)
        return base
    shares, shares_dt = latest_shares(cache)
    price = base.get("price")
    if price and shares:
        mcap, basis = price * shares, f"현재가 × 발행주식수 (SEC {shares_dt})"
    else:
        mcap, basis = base.get("market_cap"), base.get("market_cap_basis")
    ttm_lbs = fin["periods"]["ttm"]
    q_lbs = fin["periods"]["quarter"]
    ni = fin["statements"]["IS"]["items"]["net_income_owner"]["ttm"].get(ttm_lbs[-1]) if ttm_lbs else None
    eq = fin["statements"]["BS"]["items"]["equity_owner"]["quarter"].get(q_lbs[-1]) if q_lbs else None
    per = round(mcap / ni, 2) if mcap and ni and ni > 0 else None
    pbr = round(mcap / eq, 2) if mcap and eq and eq > 0 else None
    roe = fin["metrics"]["ttm"].get(ttm_lbs[-1], {}).get("roe") if ttm_lbs else None
    over = {
        "market_cap": mcap, "market_cap_basis": basis,
        "shares": shares,
        "per": per, "pbr": pbr,
        "valuation_basis": (f"현재 시가총액 / {ttm_lbs[-1]} 기준 4분기누적 지배주주순이익, 현재 시가총액 / {q_lbs[-1]} 말 지배주주지분 (SEC)"
                            if ttm_lbs and q_lbs else None),
        "roe": roe if roe is not None else base.get("roe"),
        "fiscal_month": fin["fiscal_month"],
        "fs_div": "CFS", "fs_div_label": "연결",
        "financials_ready": True,
        "name": base.get("name") or name or fin.get("entity"),
    }
    with _summary_lock:
        _summary_cache[code] = (time.time(), over)
    out = dict(base)
    out.update(over)
    return out


# ===== 공시 목록 (SEC submissions) =====
def _disc_path(code):
    return os.path.join(DISC_DIR, f"{_norm_code(code)}.json")


def disclosures(code, cik10=None, refresh=False):
    """최근 1년 10-K·10-Q·8-K·DEF 14A(정정 포함) 목록과 원문 링크. 저장본이 DISC_TTL 안이면 재사용."""
    code = _norm_code(code)
    path = _disc_path(code)
    if not refresh:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("fetched_ts", 0) < DISC_TTL:
                cached["cached"] = True
                return cached
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    if not cik10:
        cik10, _ = resolve_cik(code)
    if not cik10:
        raise NoDataError(f"SEC CIK 를 찾을 수 없음: {code}")
    data = _sec_get(SEC_SUBMISSIONS_URL.format(cik=cik10)).json()
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    rdates = recent.get("reportDate") or []
    descs = recent.get("primaryDocDescription") or []
    items8k = recent.get("items") or []
    end = date.today()
    bgn = end - timedelta(days=DISC_DAYS)
    cik_int = int(cik10)
    items = []
    for i, form in enumerate(forms):
        base_form = form[:-2] if form.endswith("/A") else form
        if base_form not in DISC_FORMS:
            continue
        fdate = dates[i] if i < len(dates) else ""
        if not fdate or fdate < bgn.isoformat() or fdate > end.isoformat():
            continue
        accn = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        rd = rdates[i] if i < len(rdates) else ""
        it8 = items8k[i] if i < len(items8k) else ""
        title = form
        if base_form in ("10-K", "10-Q") and rd:
            title += f" (기간 말 {rd})"
        elif base_form == "8-K" and it8:
            title += f" 항목 {it8}"
        elif base_form == "DEF 14A":
            title += " (주주총회 위임장)"
        if form.endswith("/A"):
            title += " 정정"
        acc_nodash = accn.replace("-", "")
        items.append({
            "date": fdate, "title": title, "form": form, "rcept_no": accn,
            "url": SEC_ARCHIVE_URL.format(cik_int=cik_int, accession=acc_nodash, doc=doc) if doc else
                   SEC_INDEX_URL.format(cik_int=cik_int, accession=acc_nodash),
            "index_url": SEC_INDEX_URL.format(cik_int=cik_int, accession=acc_nodash),
            "filer": data.get("name") or "",
            "periodic": base_form in ("10-K", "10-Q"),
            "remark": (descs[i] if i < len(descs) else "") or "",
            "report_date": rd,
        })
    items.sort(key=lambda x: (x["date"], x["rcept_no"]), reverse=True)
    out = {"market": "US", "code": code, "cik": cik10, "from": bgn.isoformat(), "to": end.isoformat(),
           "count": len(items), "forms": list(DISC_FORMS), "items": items,
           "fetched_at": _now(), "fetched_ts": time.time()}
    _atomic_write_json(path, out)
    out["cached"] = False
    return out


# ===== 월말 주가 (재무정보 차트의 주가·주가수익률 선) =====
_price_cache = {}
_price_lock = threading.Lock()


def monthly_prices(code):
    """최근 10년 월말 종가 {"YYYY.MM": 종가} — stock_profile.kr_monthly_prices 와 같은 구조. yfinance 월봉(분할 조정), 하루 메모리 캐시."""
    code = _norm_code(code)
    with _price_lock:
        hit = _price_cache.get(code)
        if hit and time.time() - hit[0] < PRICE_TTL:
            return hit[1]
    symbol = _yahoo_symbol(code)
    out = {"market": "US", "code": code, "symbol": symbol, "source": "yfinance 1mo", "currency": "USD", "prices": {}}
    try:
        import yfinance as yf
        df = yf.download(symbol, period="10y", interval="1mo", progress=False, auto_adjust=False, threads=False)
        col = df["Close"]
        if hasattr(col, "columns"):
            col = col.iloc[:, 0]
        for d, v in col.items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v == v:   # NaN 제외
                out["prices"][f"{d.year}.{d.month:02d}"] = round(v, 2)
    except Exception as e:
        print(f"[us_financials] {code} 월봉 실패: {type(e).__name__}", flush=True)
    out["count"] = len(out["prices"])
    out["fetched_at"] = _now()
    if out["prices"]:
        with _price_lock:
            _price_cache[code] = (time.time(), out)
    return out


# ===== AI 계정 대응: 규칙이 못 잡은 핵심 항목만 Claude Haiku 에 묻고, 검증을 통과한 답을 대응표(source ai)에 저장 =====
AI_MODEL = "claude-haiku-4-5-20251001"
DAILY_AI_LIMIT = 200                                   # 하루 호출 한도 (earnings_tracker.DAILY_HAIKU_LIMIT 와 같은 방식)
AI_STATE_PATH = os.path.join(concept_map.DIR, "_ai_state.json")
AI_MAX_CONCEPTS = 400                                  # 이보다 많으면 값이 0·빈 계정을 빼고 보낸다
# 빈 항목이 있으면 AI 에 묻는 핵심 항목
CORE_KEYS = ["revenue", "cogs", "operating_income", "net_income", "net_income_owner", "total_assets", "current_assets",
             "current_liabilities", "total_liabilities", "total_equity", "equity_owner", "trade_receivables", "trade_payables",
             "inventories", "ppe", "cash", "cur_fin_liab", "noncur_fin_liab", "finance_cost", "cfo", "cfi", "cff",
             "capex_ppe", "cash_end"]
# 항목 정의 한 줄 (AI 지시문용)
ITEM_DEFS = {
    "revenue": "총수익. 매출액 또는 영업수익", "cogs": "매출원가 (상품·서비스 원가)", "gross_profit": "매출총이익 = 매출액 - 매출원가",
    "sga": "운영비용 합계 (판매관리비 + 연구개발비)", "operating_income": "영업이익", "other_income": "기타 영업외 수익(순액 가능)",
    "other_expense": "기타 영업외 비용", "finance_income": "이자·투자 수익", "finance_cost": "이자비용 (손익계산서, 현금 지급액이 아님)",
    "equity_method": "지분법 손익", "pretax_income": "법인세차감전순이익", "income_tax": "법인세비용",
    "net_income": "당기순이익 (비지배지분 포함)", "net_income_owner": "지배주주(모회사 주주) 귀속 순이익",
    "net_income_minority": "비지배주주 귀속 순이익", "eps_basic": "기본 주당이익", "eps_diluted": "희석 주당이익",
    "current_assets": "유동자산 합계", "cash": "현금및현금성자산 (재무상태표)", "st_fin": "단기 투자·유가증권 (유동)",
    "lt_fin": "장기 투자·유가증권 (비유동)", "trade_receivables": "매출채권 (순액)", "inventories": "재고자산",
    "noncurrent_assets": "비유동자산 합계", "ppe": "유형자산 순액", "intangibles": "무형자산 (영업권 포함)",
    "investment_property": "투자부동산", "associates": "관계기업·지분법 투자자산", "total_assets": "자산총계",
    "current_liabilities": "유동부채 합계", "cur_fin_liab": "유동 이자부부채 (단기차입금 + 유동성장기부채 + 기업어음)",
    "trade_payables": "매입채무 (영업상 외상매입금)", "noncurrent_liabilities": "비유동부채 합계",
    "noncur_fin_liab": "비유동 이자부부채 (장기부채)", "total_liabilities": "부채총계", "equity_owner": "지배주주지분 (모회사 주주 자본)",
    "retained_earnings": "이익잉여금", "equity_minority": "비지배주주지분", "total_equity": "자본총계 (비지배 포함)",
    "cfo": "영업활동현금흐름", "depreciation": "유형자산 감가상각비", "amortization": "무형자산 상각비", "cfi": "투자활동현금흐름",
    "capex_ppe": "유형자산 취득 지출", "capex_intangible": "무형자산 취득 지출", "cff": "재무활동현금흐름",
    "dividends_paid": "배당금 지급", "treasury_purchase": "자기주식 취득", "cash_change": "현금 증감 (기간)",
    "cash_end": "기말 현금 (현금흐름표, 제한현금 포함 가능)",
}
_ai_jobs = {}
_ai_skip_today = {}       # 코드 → 오늘 날짜 (한도·조건으로 오늘은 더 묻지 않음)
_ai_state_lock = threading.Lock()


def _is_financial(cache):
    """은행·보험·증권 등 금융회사인지 (SIC 6000~6499, 6700~6799 또는 업종 설명 키워드)"""
    sic = cache.get("sic") or ""
    desc = (cache.get("sic_desc") or "").lower()
    try:
        s = int(sic)
        if 6000 <= s <= 6499 or 6700 <= s <= 6799:
            return True
    except ValueError:
        pass
    return any(w in desc for w in ("bank", "insurance", "securit", "broker", "finance", "credit", "savings", "investment"))


def _latest_value(node):
    """계정의 최근 값: 연간(350~380일) 기간 값 우선, 없으면 가장 최근 시점 값. 반환 (end, val) 또는 (None, None)"""
    best_y = best_i = None
    for start, end, val, form, filed, accn in node.get("r", []):
        if start:
            if _duration_kind(start, end) != "y":
                continue
            if best_y is None or (end, filed) > (best_y[0], best_y[2]):
                best_y = (end, val, filed)
        else:
            if best_i is None or (end, filed) > (best_i[0], best_i[2]):
                best_i = (end, val, filed)
    b = best_y or best_i
    return (b[0], b[1]) if b else (None, None)


def concept_catalog(cache, max_n=None):
    """회사가 쓰는 전체 계정 목록 [{"concept", "unit", "end", "value"}] (이름순). max_n 을 넘으면 값이 0·빈 계정을 뺀다."""
    out = []
    for cid, node in (cache.get("facts") or {}).items():
        end, val = _latest_value(node)
        out.append({"concept": _cname(cid), "unit": node.get("u"), "end": end, "value": val})
    out.sort(key=lambda x: x["concept"])
    if max_n and len(out) > max_n:
        out = [x for x in out if x["value"] not in (None, 0)]
    return out


def _ai_state():
    try:
        with open(AI_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _ai_allowed():
    """일일 한도 확인 + 카운터 증가 (earnings_tracker._check_and_increment_haiku_counter 와 같은 방식). 반환 (허용 여부, 오늘 호출 수)"""
    with _ai_state_lock:
        st = _ai_state()
        today = date.today().isoformat()
        if st.get("counter_date") != today:
            st["counter_date"], st["counter_value"] = today, 0
        if st.get("counter_value", 0) >= DAILY_AI_LIMIT:
            return False, st["counter_value"]
        st["counter_value"] = st.get("counter_value", 0) + 1
        os.makedirs(concept_map.DIR, exist_ok=True)
        _atomic_write_json(AI_STATE_PATH, st)
        return True, st["counter_value"]


AI_SYSTEM = """당신은 미국 SEC XBRL(us-gaap) 재무제표 계정 대응 전문가입니다.
한 회사가 실제로 쓰는 계정 목록이 주어집니다. 요청한 재무 항목마다 그 목록에서 가장 알맞은 계정 이름 하나를 고르세요.

규칙
- concept 은 목록에 있는 이름을 글자 그대로 쓴다. 목록에 없는 이름을 만들지 않는다.
- 알맞은 계정이 없으면 concept 을 null 로 둔다. 뜻이 다른 계정(예: 현금 지급액·세무 이자·변동액을 비용 대신)을 억지로 고르지 않는다. null 이 틀린 계정보다 낫다.
- 손익 항목은 손익계산서 계정(기간 값), 재무상태표 항목은 시점 값, 현금흐름 항목은 현금흐름표 계정을 고른다.
- 합계 항목(자산총계·부채총계·자본총계)은 구성 항목이 아니라 합계 계정이어야 한다.
- 각 답에 reason(한국어 한 줄)과 confidence(high|medium|low)를 붙인다. 뜻이 조금 다르거나 근사치면 low.
- 출력은 JSON 하나만. 설명·마크다운 금지. 형식:
{"items": {"항목키": {"concept": "계정이름 또는 null", "reason": "한 줄", "confidence": "high|medium|low"}, ...}}"""

AI_FINANCIAL_NOTE = """이 회사는 금융회사(은행·보험·증권)입니다.
- revenue(매출액) 자리에는 총수익을 고른다: Revenues 가 있으면 그것, 없으면 순이자수익(InterestIncomeExpenseNet)과 비이자수익(NoninterestIncome) 중 총수익에 가장 가까운 것. 없으면 null.
- operating_income(영업이익) 자리는 세전이익(IncomeLossFromContinuingOperationsBeforeIncomeTaxes...)으로 대체할 수 있다(confidence medium).
- cogs(매출원가)·gross_profit·inventories(재고)·current_assets·current_liabilities·trade_payables 는 금융회사에 없는 것이 정상이므로 null 이 맞다."""


def _ai_user_message(cache, keys, catalog):
    items = "\n".join(f"- {k}: {ITEM_DEFS.get(k, k)}" for k in keys)
    rows = "\n".join(f"{c['concept']} | {c['unit']} | {c['end'] or ''} | {c['value'] if c['value'] is not None else ''}" for c in catalog)
    fin = ("\n\n" + AI_FINANCIAL_NOTE) if _is_financial(cache) else ""
    return (f"회사: {cache.get('entity') or cache.get('code')} ({cache.get('code')})\n"
            f"업종(SEC SIC): {cache.get('sic') or '?'} {cache.get('sic_desc') or '알 수 없음'}{fin}\n\n"
            f"대응할 항목 (키: 정의)\n{items}\n\n"
            f"회사가 쓰는 계정 목록 ({len(catalog)}개; 계정 | 단위 | 최근 기간 말 | 최근 연간 값)\n{rows}")


def _call_ai(system, user):
    """Claude Haiku 호출 (earnings_tracker 의 클라이언트 재사용). 반환 (본문 텍스트, 입력 토큰, 출력 토큰). 키는 로그에 남기지 않는다."""
    from earnings_tracker import _get_anthropic_client
    client = _get_anthropic_client()
    res = client.messages.create(model=AI_MODEL, max_tokens=2000, system=system, messages=[{"role": "user", "content": user}])
    text = "".join(getattr(b, "text", "") for b in res.content).strip()
    usage = getattr(res, "usage", None)
    return text, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def _parse_ai_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("JSON 없음")
    d = json.loads(text[i:j + 1])
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, dict):
        raise ValueError("items 없음")
    return items


def _within(a, b, pct=0.01):
    return a is not None and b is not None and abs(a - b) <= pct * max(abs(a), abs(b), 1)


def validate_answers(cache, answers, current):
    """AI 답 검증. answers: {키: {"concept","reason","confidence"}}, current: {키: 계정} (규칙이 이미 고른 것).
    반환 (통과 {키: 답}, 거절 [(키, concept, 이유)]). 검증 실패 항목은 버린다."""
    facts = cache.get("facts") or {}
    kinds = {key: (sheet, kind) for sheet, key, label, kind, cands in RULES}
    ok, rejected = {}, []
    for key, a in answers.items():
        if key not in kinds:
            rejected.append((key, a.get("concept"), "모르는 항목 키"))
            continue
        c = a.get("concept")
        if c in (None, "", "null"):
            ok[key] = {"concept": None, "reason": a.get("reason", ""), "confidence": a.get("confidence", "medium")}
            continue
        cid = _cid(c)
        if cid not in facts:
            rejected.append((key, c, "회사 계정 목록에 없음"))
            continue
        unit = facts[cid].get("u")
        want = "USD/shares" if kinds[key][1] == "eps" else "USD"
        if unit != want:
            rejected.append((key, c, f"단위 불일치 ({unit}, 필요 {want})"))
            continue
        ok[key] = {"concept": cid, "reason": a.get("reason", ""), "confidence": a.get("confidence", "medium") or "medium"}

    # 합계 대조: 규칙이 고른 계정 + AI 답을 합쳐 최근 연간 값으로 확인
    merged = {k: _cid(v) for k, v in current.items() if v}
    merged.update({k: v["concept"] for k, v in ok.items() if v["concept"]})

    def val(key):
        cid = merged.get(key)
        return _latest_value(facts[cid])[1] if cid and cid in facts else None

    def drop(keys, why):
        for k in keys:
            if k in ok and ok[k]["concept"]:
                rejected.append((k, _cname(ok[k]["concept"]), why))
                del ok[k]

    rev, cogs, gp = val("revenue"), val("cogs"), val("gross_profit")
    if None not in (rev, cogs, gp) and not _within(rev - cogs, gp):
        drop(["revenue", "cogs", "gross_profit"], f"매출액 - 매출원가 ≠ 매출총이익 ({rev:,} - {cogs:,} vs {gp:,})")
    ta, tl, te = val("total_assets"), val("total_liabilities"), val("total_equity")
    if None not in (ta, tl, te) and not _within(ta, tl + te):
        drop(["total_assets", "total_liabilities", "total_equity"], f"자산총계 ≠ 부채 + 자본 ({ta:,} vs {tl + te:,})")
    ca, ta = val("current_assets"), val("total_assets")
    if None not in (ca, ta) and ca >= ta:
        drop(["current_assets"], f"유동자산({ca:,}) ≥ 자산총계({ta:,})")
    cl, tl = val("current_liabilities"), val("total_liabilities")
    if None not in (cl, tl) and cl > tl:
        drop(["current_liabilities"], f"유동부채({cl:,}) > 부채총계({tl:,})")
    for key in ("cash", "cash_end", "ppe", "inventories", "trade_receivables", "trade_payables", "total_assets"):
        v = val(key)
        if v is not None and v < 0:
            drop([key], f"음수 ({v:,})")
    # 현금흐름 부호: 유형자산 취득은 양수·음수 모두 허용(절댓값 처리). 영업현금흐름이 자산총계보다 크면 이상
    cfo = val("cfo")
    if cfo is not None and ta is not None and abs(cfo) > ta:
        drop(["cfo"], f"영업현금흐름({cfo:,}) 절댓값이 자산총계보다 큼")
    return ok, rejected


def _pending_keys(out):
    """AI 에 물을 항목: 핵심 항목 중 전 기간 값이 없고(missing), 대응표에 ai·user 답이 없는 것 (계산으로 채워진 항목은 제외)"""
    cu = out.get("concepts_used") or {}
    missing = {m["key"] for m in out.get("missing") or []}
    return [k for k in CORE_KEYS if k in missing and k in cu and cu[k].get("source") == "rule"]


def _ai_needed(code, out):
    """AI 에 물을 핵심 항목이 있는지 (대응표에 답이 없고, 오늘 아직 묻지 않았을 때). AI 작업이 돌고 있으면 True(진행 표시)."""
    with _jobs_guard:
        job = _ai_jobs.get(code)
        if job and job["state"] == "running":
            return True
        if job:
            _ai_jobs.pop(code, None)
    today = date.today().isoformat()
    if _ai_skip_today.get(code) == today:
        return False
    cm = concept_map.load("US", code)
    if cm.get("ai_asked") == today:
        return False
    return bool(_pending_keys(out))


def _start_ai(code):
    with _jobs_guard:
        job = _ai_jobs.get(code)
        if job and job["state"] == "running":
            return job
        job = {"state": "running", "started_at": _now(), "result": None}
        _ai_jobs[code] = job

    def run():
        try:
            job["result"] = ai_fill(code)
        except Exception as e:
            job["result"] = {"called": False, "error": f"{type(e).__name__}: {e}"}
            print(f"[us_financials] {code} AI 대응 에러: {type(e).__name__}: {e}", flush=True)
        job["state"] = "done"
    threading.Thread(target=run, daemon=True).start()
    return job


def ai_fill(code, cache=None, force=False, dry_run=False):
    """규칙이 못 잡은 핵심 항목을 AI 에 묻고 검증을 통과한 답을 대응표에 저장. 하루 한 번만 묻는다.
    반환: {"called", "reason", "asked", "accepted", "rejected", "tokens", "sent_concepts"}"""
    code = _norm_code(code)
    cache = cache or _load_cache(code)
    if not cache or not cache.get("facts"):
        return {"called": False, "reason": "저장본 없음"}
    today = date.today().isoformat()
    cm = concept_map.load("US", code)
    if cm.get("ai_asked") == today and not force:
        return {"called": False, "reason": "오늘 이미 물었음"}
    out = compute(cache, 10, cache.get("entity", ""))
    cu = out["concepts_used"]
    pending = _pending_keys(out)
    if not pending:
        return {"called": False, "reason": "빈 핵심 항목 없음"}
    allowed, n = _ai_allowed()
    if not allowed:
        _ai_skip_today[code] = today
        print(f"[us_financials] AI 일일 한도({DAILY_AI_LIMIT}) 도달, {code} 계정 대응 skip (빈 항목 {len(pending)}개)", flush=True)
        return {"called": False, "reason": f"일일 한도 {DAILY_AI_LIMIT} 도달", "asked": pending}
    catalog = concept_catalog(cache, AI_MAX_CONCEPTS)
    current = {k: v["concept"] for k, v in cu.items() if v.get("concept")}
    user = _ai_user_message(cache, pending, catalog)
    if dry_run:
        return {"called": False, "reason": "dry_run", "asked": pending, "system": AI_SYSTEM, "user": user, "sent_concepts": len(catalog)}
    concept_map.mark_asked("US", code, today)
    tokens = {"input": 0, "output": 0}
    answers, err = None, None
    for attempt in range(2):                          # 파싱 실패 시 1회 재시도
        try:
            text, ti, to = _call_ai(AI_SYSTEM, user)
            tokens["input"] += ti or 0
            tokens["output"] += to or 0
            answers = _parse_ai_json(text)
            break
        except Exception as e:                        # 예외 문자열에 키가 들어가지 않도록 종류만 남긴다
            err = f"{type(e).__name__}"
            print(f"[us_financials] {code} AI 응답 파싱/호출 실패 ({attempt + 1}/2): {err}", flush=True)
    if answers is None:
        return {"called": True, "reason": f"AI 응답 실패: {err}", "asked": pending, "tokens": tokens, "sent_concepts": len(catalog)}
    answers = {k: v for k, v in answers.items() if k in pending and isinstance(v, dict)}
    ok, rejected = validate_answers(cache, answers, current)
    for k, c, why in rejected:
        print(f"[us_financials] {code} AI 답 거절 {k}={c}: {why}", flush=True)
    meta = {"asked": pending, "sent_concepts": len(catalog), "tokens": tokens, "accepted": {k: _cname(v["concept"]) if v["concept"] else None for k, v in ok.items()},
            "rejected": [(k, c, why) for k, c, why in rejected], "daily_count": n}
    concept_map.set_ai("US", code, ok, meta)
    print(f"[us_financials] {code} AI 계정 대응: 물음 {len(pending)}개, 저장 {len(ok)}개, 거절 {len(rejected)}개, "
          f"토큰 {tokens['input']}+{tokens['output']}, 오늘 {n}/{DAILY_AI_LIMIT}", flush=True)
    return {"called": True, "asked": pending, "accepted": meta["accepted"], "rejected": meta["rejected"], "tokens": tokens,
            "sent_concepts": len(catalog), "answers": {k: v for k, v in answers.items()}}


def concept_map_view(code):
    """GET /api/stock/concept-map 응답: 대응표 전체 + 회사 계정 목록(이름·단위·최근 값)"""
    code = _norm_code(code)
    cache = _load_cache(code)
    cm = concept_map.load("US", code)
    items = {}
    for k, v in cm.get("items", {}).items():
        e = dict(v)
        if e.get("concept"):
            e["concept"] = _cname(_cid(e["concept"]))
        if e.get("prev") and e["prev"].get("concept"):
            e["prev"] = dict(e["prev"], concept=_cname(_cid(e["prev"]["concept"])))
        items[k] = e
    labels = {key: label for sheet, key, label, kind, cands in RULES if key not in HIDDEN_KEYS}
    return {"market": "US", "code": code, "items": items, "ai_asked": cm.get("ai_asked"), "ai_log": cm.get("ai_log", [])[-3:],
            "keys": [{"key": k, "label": lb, "definition": ITEM_DEFS.get(k, "")} for k, lb in labels.items()],
            "concepts": concept_catalog(cache) if cache else [], "entity": (cache or {}).get("entity"),
            "sic": (cache or {}).get("sic"), "sic_desc": (cache or {}).get("sic_desc")}


def set_user_concept(code, key, concept, years=5, name=""):
    """PUT: 사용자 지정(concept None = 해당 없음) 뒤 다시 계산한 응답. 계정이 저장본에 없으면 ValueError."""
    code = _norm_code(code)
    labels = {k for sheet, k, label, kind, cands in RULES if k not in HIDDEN_KEYS}
    if key not in labels:
        raise ValueError(f"모르는 항목: {key}")
    cache = _load_cache(code)
    if not cache:
        raise ValueError("저장본 없음 — 먼저 재무정보를 조회하세요")
    if concept is not None:
        cid = _cid(concept)
        if cid not in (cache.get("facts") or {}):
            raise ValueError(f"회사 계정 목록에 없음: {concept}")
        concept = cid
    concept_map.set_user("US", code, key, concept)
    out = compute(cache, years, name)
    out.update(status="ready", updating=None)
    return out


def delete_user_concept(code, key, years=5, name=""):
    code = _norm_code(code)
    cache = _load_cache(code)
    if not cache:
        raise ValueError("저장본 없음")
    _, removed = concept_map.delete_user("US", code, key)
    out = compute(cache, years, name)
    out.update(status="ready", updating=None, removed=removed)
    return out
