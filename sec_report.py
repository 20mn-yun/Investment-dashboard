import os
import re
import json
import time
import subprocess
import threading
from datetime import datetime
from uuid import uuid4

import requests

import telegram_report

SEC_USER_AGENT = "InvestmentDashboard changyun1222@gmail.com"
SEC_MIN_INTERVAL = 0.15
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{doc}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CIK_MAP_PATH = os.path.join(CACHE_DIR, "sec_cik_map.json")
CIK_MAP_TTL = 7 * 24 * 3600

TARGET_FORMS = ("10-K", "10-Q")
FINANCIAL_CONCEPTS = [
    ("Revenues", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]),
    ("NetIncomeLoss", ["NetIncomeLoss"]),
    ("EarningsPerShareDiluted", ["EarningsPerShareDiluted"]),
    ("OperatingIncomeLoss", ["OperatingIncomeLoss"]),
    ("Assets", ["Assets"]),
    ("StockholdersEquity", ["StockholdersEquity"]),
]

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')
_IX_HEADER_RE = re.compile(r"<ix:header\b.*?</ix:header>", re.IGNORECASE | re.DOTALL)
_HIDDEN_RE = re.compile(
    r"<(\w+)\b[^>]*style\s*=\s*([\"'])[^\"']*display\s*:\s*none[^\"']*\2[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

_last_request_ts = 0.0
_request_lock = threading.Lock()


def _safe_filename(name):
    return _UNSAFE_CHARS.sub("_", name)


def _sec_get(url, timeout=30):
    global _last_request_ts
    with _request_lock:
        wait = SEC_MIN_INTERVAL - (time.time() - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
        res = requests.get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
        _last_request_ts = time.time()
    res.raise_for_status()
    return res


def _load_cik_map():
    if os.path.exists(CIK_MAP_PATH):
        try:
            with open(CIK_MAP_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - float(cached.get("fetched_at", 0)) < CIK_MAP_TTL and cached.get("map"):
                return cached["map"]
        except Exception:
            pass
    res = _sec_get(SEC_TICKERS_URL)
    data = res.json()
    mapping = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper()
        if ticker:
            mapping[ticker] = {"cik": int(entry.get("cik_str", 0)), "title": entry.get("title", "")}
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CIK_MAP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "map": mapping}, f, ensure_ascii=False)
    os.replace(tmp, CIK_MAP_PATH)
    return mapping


def get_cik(ticker):
    mapping = _load_cik_map()
    entry = mapping.get(str(ticker).upper())
    if not entry:
        return None, None
    return f"{entry['cik']:010d}", entry.get("title", "")


def list_filings(cik10, date_from, date_to):
    res = _sec_get(SEC_SUBMISSIONS_URL.format(cik=cik10))
    data = res.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    periods = recent.get("reportDate", [])
    results = []
    for i, form in enumerate(forms):
        if form not in TARGET_FORMS:
            continue
        fdate = dates[i] if i < len(dates) else ""
        if not fdate or fdate < date_from or fdate > date_to:
            continue
        results.append({
            "form": form,
            "filingDate": fdate,
            "reportDate": periods[i] if i < len(periods) else "",
            "accession": accessions[i] if i < len(accessions) else "",
            "primaryDocument": docs[i] if i < len(docs) else "",
        })
    results.sort(key=lambda r: r["filingDate"])
    return results


def _clean_html(html):
    html = _IX_HEADER_RE.sub("", html)
    html = _SCRIPT_STYLE_RE.sub("", html)
    prev = None
    while prev != html:
        prev = html
        html = _HIDDEN_RE.sub("", html)
    return html


def html_to_markdown(html):
    try:
        import html2text
    except ImportError:
        raise RuntimeError("html2text 모듈이 없습니다. 터미널에서 pip3 install html2text 를 실행하세요")
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    converter.ignore_emphasis = False
    return _tidy_markdown(converter.handle(_clean_html(html)))


_EMPTY_TABLE_ROW_RE = re.compile(r"^[\s|]+$")


def _tidy_markdown(md):
    cleaned = []
    blank_run = 0
    for line in md.splitlines():
        if _EMPTY_TABLE_ROW_RE.match(line) and "|" in line and "-" not in line:
            continue
        if line.strip() == "":
            blank_run += 1
            if blank_run > 2:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned).rstrip() + "\n"


def download_filing_markdown(cik10, filing, save_dir, filename_base):
    accession = filing["accession"].replace("-", "")
    doc = filing["primaryDocument"]
    if not accession or not doc:
        return None
    url = SEC_ARCHIVE_URL.format(cik_int=int(cik10), accession=accession, doc=doc)
    res = _sec_get(url, timeout=60)
    md = html_to_markdown(res.text)
    header = (
        f"# {filing['form']} — filed {filing['filingDate']}"
        f"{' (period ' + filing['reportDate'] + ')' if filing.get('reportDate') else ''}\n\n"
        f"Source: {url}\n\n---\n\n"
    )
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, filename_base + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + md)
    return path


def _extract_quarterly(facts_units):
    rows = []
    for unit, items in facts_units.items():
        for it in items:
            form = it.get("form", "")
            if form not in TARGET_FORMS:
                continue
            start = it.get("start")
            end = it.get("end")
            if not end:
                continue
            if start:
                try:
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                except ValueError:
                    continue
                if days > 100:
                    continue
            rows.append({
                "end": end,
                "start": start,
                "value": it.get("val"),
                "unit": unit,
                "form": form,
                "fy": it.get("fy"),
                "fp": it.get("fp"),
                "filed": it.get("filed"),
                "frame": it.get("frame"),
            })
    dedup = {}
    for r in rows:
        key = (r["end"], r["start"])
        prev = dedup.get(key)
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            dedup[key] = r
    return sorted(dedup.values(), key=lambda r: (r["end"], r["start"] or ""))


def build_financials_json(cik10, ticker, save_dir):
    res = _sec_get(SEC_COMPANYFACTS_URL.format(cik=cik10), timeout=60)
    data = res.json()
    gaap = data.get("facts", {}).get("us-gaap", {})
    out = {
        "ticker": ticker,
        "cik": cik10,
        "entityName": data.get("entityName", ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": SEC_COMPANYFACTS_URL.format(cik=cik10),
        "concepts": {},
    }
    for name, candidates in FINANCIAL_CONCEPTS:
        for concept in candidates:
            node = gaap.get(concept)
            if not node or not node.get("units"):
                continue
            series = _extract_quarterly(node["units"])
            if not series:
                continue
            out["concepts"][name] = {
                "concept": concept,
                "label": node.get("label", ""),
                "description": node.get("description", ""),
                "quarterly": series,
            }
            break
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{ticker}_financials.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return path, len(out["concepts"])


_jobs = {}


def get_job(job_id):
    return _jobs.get(job_id)


def start_download_job(ticker, date_from, date_to, want_md=True, want_json=True):
    job_id = uuid4().hex[:8]
    _jobs[job_id] = {
        "status": "searching",
        "ticker": str(ticker).upper(),
        "company": "",
        "found": 0,
        "done": 0,
        "files": [],
        "skipped": 0,
        "download_path": "",
        "error": None,
        "stop_requested": False,
    }
    t = threading.Thread(
        target=_run_download_job,
        args=(job_id, str(ticker).upper(), date_from, date_to, want_md, want_json),
        daemon=True,
    )
    t.start()
    return job_id


def _listdir_safe(path):
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _run_download_job(job_id, ticker, date_from, date_to, want_md, want_json):
    job = _jobs[job_id]
    try:
        cik10, company = get_cik(ticker)
        if not cik10:
            raise RuntimeError(f"SEC에서 티커 {ticker}의 CIK를 찾을 수 없습니다")
        job["company"] = company

        final_root = telegram_report.drive_path("Analysis", ticker, "SEC보고서")
        if not final_root:
            raise RuntimeError("구글 드라이브 마운트를 찾을 수 없습니다")
        final_md = os.path.join(final_root, "MD")
        final_json = os.path.join(final_root, "JSON")
        staging_root = os.path.join(BASE_DIR, "downloads", "sec", ticker)
        staging_md = os.path.join(staging_root, "MD")
        staging_json = os.path.join(staging_root, "JSON")
        os.makedirs(staging_root, exist_ok=True)

        filings = list_filings(cik10, date_from, date_to) if want_md else []
        job["found"] = len(filings)

        existing_md = _listdir_safe(final_md) if want_md else set()
        job["status"] = "downloading"

        for filing in filings:
            if job["stop_requested"]:
                job["status"] = "stopping"
                break
            fname_base = _safe_filename(f"{ticker}_{filing['form']}_{filing['filingDate']}")
            if fname_base + ".md" in existing_md:
                job["skipped"] += 1
                job["done"] += 1
                continue
            path = download_filing_markdown(cik10, filing, staging_md, fname_base)
            if path:
                job["files"].append({
                    "filename": os.path.basename(path),
                    "form": filing["form"],
                    "filingDate": filing["filingDate"],
                    "kind": "md",
                })
            job["done"] += 1

        if want_json and not job["stop_requested"]:
            job["status"] = "financials"
            path, n_concepts = build_financials_json(cik10, ticker, staging_json)
            job["files"].append({
                "filename": os.path.basename(path),
                "form": f"XBRL {n_concepts}개 계정",
                "filingDate": datetime.now().strftime("%Y-%m-%d"),
                "kind": "json",
            })

        job["status"] = "copying"
        r_mkdir = subprocess.run(["/bin/mkdir", "-p", final_root], capture_output=True, text=True)
        if r_mkdir.returncode != 0:
            raise RuntimeError(f"mkdir 실패: {r_mkdir.stderr}")

        has_payload = any(os.path.isdir(os.path.join(staging_root, d)) for d in ("MD", "JSON"))
        if has_payload:
            r_cp = subprocess.run(["/bin/cp", "-R", staging_root + "/.", final_root + "/"], capture_output=True, text=True)
            if r_cp.returncode != 0:
                raise RuntimeError(f"cp 실패: {r_cp.stderr} (staging 보존: {staging_root})")

        subprocess.run(["/bin/rm", "-rf", staging_root], capture_output=True)
        parent = os.path.dirname(staging_root)
        if os.path.isdir(parent) and not os.listdir(parent):
            subprocess.run(["/bin/rm", "-rf", parent], capture_output=True)

        job["download_path"] = final_root
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


if __name__ == "__main__":
    import sys
    cik, name = get_cik(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
    print(f"CIK: {cik}  company: {name}")
