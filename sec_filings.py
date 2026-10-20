"""SEC 8-K 실적 보도자료(Item 2.02, EX-99.1/EX-99.2) 수집·텍스트화·요약 (B).

- 목록: submissions JSON → form 8-K & items 에 2.02 → 접수별 <accession>-index.html 의 Type 열에서 EX-99.1/99.2 만 (5MB 초과 제외)
- 저장: cache/sec_filings/<티커>/<accession>_<exhibit>.htm + .md(태그 제거, 표는 행마다 탭 구분), index.json(accession 기준 중복 방지)
- Drive: <종목 폴더>/IR/<짧은이름>_<yymmdd>_8-K_<EX-99.1|EX-99.2>_<description 40자>.md + .htm
- 요약: llm_client task ir_release (sonnet, json_mode) — 접수당 1회(EX-99.1 우선), ai_budget 'ir_release' 20/일. 숫자는 원문 그대로(환산 없음)
SEC 호출은 us_financials._sec_get(User-Agent·간격·재시도) 재사용.
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

import ai_budget
import telegram_report
import us_financials as uf

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "sec_filings")
DRIVE_SUBFOLDER = "IR"
EXHIBIT_TYPES = ("EX-99.1", "EX-99.2")
MAX_EXHIBIT_BYTES = 5 * 1024 * 1024
FIRST_RUN_MAX_EXHIBITS = 8
SUMMARY_INPUT_CHARS = 12_000
_lock = threading.Lock()
_sec_calls = [0]          # 이번 프로세스의 SEC 요청 수 (검증·보고용)

SUMMARY_SYSTEM = """당신은 미국 상장사의 실적 보도자료(8-K EX-99.1)를 구조화하는 분석 보조자입니다. 주어진 텍스트만 근거로 JSON 하나를 만듭니다.

규칙
- 숫자는 환산하지 말고 원문 표기 그대로(예: "$46.7 billion", "57,006 million"). 텍스트에 없는 값은 쓰지 않는다.
- 분기는 회사가 쓰는 표기 그대로(예: "Q2 FY2027", "second quarter 2026").
- 각 항목은 한국어 한 문장, 숫자 포함. 실적핵심·세그먼트·특이사항은 5개 이하, 가이던스는 각 3개 이하.
- 출력은 JSON 하나만. 설명·마크다운·코드펜스 금지. 형식:
{"분기": "", "실적핵심": ["", ""], "가이던스": {"다음분기": [""], "연간": [""]}, "세그먼트": [""], "특이사항": [""], "숫자단위": "원문에 쓰인 단위(예: millions USD)"}"""


def _code(code):
    return uf._norm_code(code)


def _dir(code):
    return os.path.join(CACHE_DIR, _code(code))


def _index_path(code):
    return os.path.join(_dir(code), "index.json")


def _load_index(code):
    try:
        with open(_index_path(code), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(code, idx):
    os.makedirs(_dir(code), exist_ok=True)
    tmp = _index_path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _index_path(code))


def _get(url, timeout=60):
    _sec_calls[0] += 1
    return uf._sec_get(url, timeout=timeout)


def _parse_size(s):
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


# ===== 목록 =====
def _norm_exhibit(t):
    """Type 열 표기 통일: 'EX-99' → EX-99.1, 'EX-99.01' → EX-99.1, 'EX-99.02' → EX-99.2 (회사마다 표기가 다름)"""
    t = (t or "").strip().upper().replace(" ", "")
    m = re.match(r"^EX-99(?:\.0?(\d+))?$", t)
    if not m:
        return t
    n = m.group(1)
    return f"EX-99.{int(n)}" if n else "EX-99.1"


def _exhibits_from_index_html(cik_int, acc):
    """<accession>-index.html 의 문서 표에서 (Seq, Description, Document, Type, Size) → EX-99.1/99.2 만"""
    from bs4 import BeautifulSoup
    acc_nodash = acc.replace("-", "")
    url = uf.SEC_INDEX_URL.format(cik_int=cik_int, accession=acc_nodash) + f"{acc}-index.html"
    html = _get(url, timeout=30).text
    soup = BeautifulSoup(html, "lxml")
    out = []
    for tr in soup.select("table.tableFile tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        typ = _norm_exhibit(tds[3].get_text(strip=True))
        if typ not in EXHIBIT_TYPES:
            continue
        a = tds[2].find("a")
        href = a.get("href") if a else ""
        fname = (href or "").rsplit("/", 1)[-1]
        if "/ix?doc=" in (href or ""):                  # 인라인 XBRL 뷰어 링크 → 실제 파일명
            fname = href.split("/")[-1]
        size = _parse_size(tds[4].get_text(strip=True))
        if not fname or not fname.lower().endswith((".htm", ".html", ".pdf", ".txt")):
            continue
        if size and size > MAX_EXHIBIT_BYTES:
            continue
        out.append({"exhibit": typ, "filename": fname, "description": tds[1].get_text(" ", strip=True)[:120], "size": size,
                    "url": uf.SEC_ARCHIVE_URL.format(cik_int=cik_int, accession=acc_nodash, doc=fname)})
    return out


def list_earnings_8k(code, cik10, since_days=400):
    """최근 since_days 의 8-K(Item 2.02) 접수와 EX-99.1/99.2 첨부 → [{accession, date, exhibit, filename, url, size, description}]"""
    sub = _get(uf.SEC_SUBMISSIONS_URL.format(cik=cik10), timeout=30).json()
    cik_int = int(cik10)
    rec = sub.get("filings", {}).get("recent", {})
    forms, dates, accs, items = rec.get("form") or [], rec.get("filingDate") or [], rec.get("accessionNumber") or [], rec.get("items") or []
    since = (date.today() - timedelta(days=since_days)).isoformat()
    out = []
    for i, form in enumerate(forms):
        if form != "8-K" or i >= len(dates) or dates[i] < since:
            continue
        if "2.02" not in (items[i] if i < len(items) else ""):
            continue
        acc = accs[i]
        try:
            exs = _exhibits_from_index_html(cik_int, acc)
        except Exception as e:
            print(f"[sec_filings] {code} {acc} index 읽기 실패: {type(e).__name__}", flush=True)
            continue
        for ex in exs:
            out.append(dict(ex, accession=acc, date=dates[i], cik_int=cik_int))
    out.sort(key=lambda x: (x["date"], x["accession"], x["exhibit"]), reverse=True)
    return out


# ===== 텍스트화 =====
def html_to_text(html):
    """태그 제거. 표는 행마다 셀을 탭으로 이어 한 줄. 빈 줄 3개 이상은 2개로."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c not in ("", "$", ")", "%")]
            if cells:
                rows.append("\t".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n")
    text = soup.get_text("\n")
    text = re.sub(r"[  ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_exhibit(code, entry):
    """첨부 htm 저장 + .md 텍스트. 반환 (htm 경로, md 경로, 텍스트)"""
    d = _dir(code)
    os.makedirs(d, exist_ok=True)
    base = f"{entry['accession']}_{entry['exhibit'].replace('EX-', 'EX')}"
    htm_path, md_path = os.path.join(d, base + ".htm"), os.path.join(d, base + ".md")
    if os.path.exists(htm_path) and os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            return htm_path, md_path, f.read()
    res = _get(entry["url"], timeout=60)
    raw = res.content
    with open(htm_path, "wb") as f:
        f.write(raw)
    if entry["filename"].lower().endswith(".pdf"):
        text = ""
        try:
            from pypdf import PdfReader
            r = PdfReader(htm_path)
            text = "\n\n".join((p.extract_text() or "") for p in r.pages[:30])
        except Exception:
            pass
    else:
        text = html_to_text(raw.decode("utf-8", errors="ignore"))
    header = f"# {code} 8-K {entry['exhibit']} ({entry['date']}) — {entry.get('description') or ''}\n출처: {entry['url']}\n\n"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(header + text + "\n")
    return htm_path, md_path, text


# ===== Drive =====
def _drive_dir(code, name):
    base = telegram_report.report_download_base()
    if not base:
        return None
    kw = telegram_report.stock_folder_keyword("US", _code(code), name or None)
    return os.path.join(telegram_report.stock_folder(base, kw), DRIVE_SUBFOLDER)


def _drive_name(code, name, entry):
    short = telegram_report._safe_name(telegram_report.us_short_name(_code(code), name or None), 30)
    desc = telegram_report._safe_name(re.sub(r"[\s_]+", "_", entry.get("description") or entry["exhibit"]), 40)
    return f"{short}_{entry['date'][2:].replace('-', '')}_8-K_{entry['exhibit']}_{desc}"


def export_drive(code, name, entry, htm_path, md_path):
    d = _drive_dir(code, name)
    if not d:
        return {"ok": False, "error": "Drive 를 찾지 못함"}
    try:
        os.makedirs(d, exist_ok=True)
        base = os.path.join(d, _drive_name(code, name, entry))
        import shutil
        for src, ext in ((md_path, ".md"), (htm_path, ".htm")):
            dst = base + ext
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        return {"ok": True, "path": base + ".md"}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


# ===== 요약 =====
def _summary_path(code, acc):
    return os.path.join(_dir(code), f"{acc}_summary.json")


def summarize(code, name, entry, text, limit=None):
    """접수 1건 요약 (ir_release). 반환 summary dict 또는 {"status": ...}"""
    import llm_client
    p = _summary_path(code, entry["accession"])
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cur = json.load(f)
            if cur.get("status") == "ok":
                return cur
        except (OSError, json.JSONDecodeError):
            pass
    allowed, n = ai_budget.take("ir_release", limit=limit)
    if not allowed:
        return {"status": "ai_limit"}
    user = f"회사: {name or code} ({code})\n접수일: {entry['date']}\n첨부: {entry['exhibit']} {entry.get('description') or ''}\n\n{text[:SUMMARY_INPUT_CHARS]}"
    t0 = time.time()
    r = llm_client.complete("ir_release", user, system=SUMMARY_SYSTEM, json_mode=True)
    if not r.ok or not isinstance(r.data, dict):
        out = {"status": f"ai_error:{(r.error or 'unknown')[:80]}", "at": datetime.now().isoformat(timespec="seconds")}
    else:
        s = r.data
        out = {"status": "ok", "accession": entry["accession"], "exhibit": entry["exhibit"], "date": entry["date"],
               "summary": {"분기": str(s.get("분기") or ""), "실적핵심": [str(x) for x in (s.get("실적핵심") or [])][:5],
                           "가이던스": {"다음분기": [str(x) for x in ((s.get("가이던스") or {}).get("다음분기") or [])][:3],
                                       "연간": [str(x) for x in ((s.get("가이던스") or {}).get("연간") or [])][:3]},
                           "세그먼트": [str(x) for x in (s.get("세그먼트") or [])][:5], "특이사항": [str(x) for x in (s.get("특이사항") or [])][:5],
                           "숫자단위": str(s.get("숫자단위") or "")},
               "tokens": {"input": (r.input_tokens or 0) + (r.cache_tokens or 0), "output": r.output_tokens}, "model": r.model,
               "seconds": round(time.time() - t0, 1), "at": datetime.now().isoformat(timespec="seconds")}
    os.makedirs(_dir(code), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


# ===== 배치 =====
def ensure_recent(code, cik10=None, name="", since_days=400, summary_limit=None):
    """신규 접수의 EX-99 받기 → Drive → 요약(EX-99.1 우선, 접수당 1회). 반환 {new, exhibits, summarized, sec_calls, errors}"""
    code = _code(code)
    cik10 = cik10 or uf.resolve_cik(code)[0]
    if not cik10:
        return {"error": "cik 없음"}
    calls0 = _sec_calls[0]
    with _lock:
        idx = _load_index(code)
        first_run = not idx
        try:
            entries = list_earnings_8k(code, cik10, since_days)
        except Exception as e:
            return {"error": f"목록 실패 {type(e).__name__}", "sec_calls": _sec_calls[0] - calls0}
        new_entries = [e for e in entries if e["accession"] not in idx or e["exhibit"] not in (idx[e["accession"]].get("exhibits") or {})]
        if first_run:
            new_entries = new_entries[:FIRST_RUN_MAX_EXHIBITS]
        n_new, n_sum, errors = 0, 0, []
        for e in new_entries:
            try:
                htm, md, text = fetch_exhibit(code, e)
                dr = export_drive(code, name, e, htm, md)
                ent = idx.setdefault(e["accession"], {"date": e["date"], "exhibits": {}})
                ent["exhibits"][e["exhibit"]] = {"filename": e["filename"], "description": e.get("description"), "size": e.get("size"),
                                                 "url": e["url"], "htm": os.path.relpath(htm, _BD), "md": os.path.relpath(md, _BD),
                                                 "drive": dr.get("path"), "drive_error": dr.get("error"), "chars": len(text),
                                                 "saved_at": datetime.now().isoformat(timespec="seconds")}
                n_new += 1
                _save_index(code, idx)
            except Exception as ex:
                errors.append(f"{e['accession']} {e['exhibit']}: {type(ex).__name__}")
        # 요약: 접수마다 EX-99.1(없으면 EX-99.2) 1회
        for acc, ent in sorted(idx.items(), key=lambda kv: kv[1].get("date") or "", reverse=True):
            if ent.get("summary_status") == "ok":
                continue
            ex = ent["exhibits"].get("EX-99.1") or ent["exhibits"].get("EX-99.2")
            if not ex or not ex.get("md"):
                continue
            try:
                with open(os.path.join(_BD, ex["md"]), "r", encoding="utf-8") as f:
                    text = f.read()
                entry = {"accession": acc, "exhibit": "EX-99.1" if ent["exhibits"].get("EX-99.1") else "EX-99.2", "date": ent.get("date"), "description": ex.get("description")}
                s = summarize(code, name, entry, text, summary_limit)
                ent["summary_status"] = s.get("status")
                if s.get("status") == "ok":
                    n_sum += 1
                elif s.get("status") == "ai_limit":
                    break
            except Exception as ex2:
                errors.append(f"{acc} 요약: {type(ex2).__name__}")
            _save_index(code, idx)
        return {"new": n_new, "exhibits": sum(len(v.get("exhibits") or {}) for v in idx.values()), "accessions": len(idx),
                "summarized": n_sum, "sec_calls": _sec_calls[0] - calls0, "errors": errors[:5]}


def status(code, cik10=None):
    """저장본 기준 {items:[{date, accession, exhibit, description, filename, drive, summary|None}]} (네트워크 없음)"""
    code = _code(code)
    idx = _load_index(code)
    items = []
    for acc, ent in sorted(idx.items(), key=lambda kv: kv[1].get("date") or "", reverse=True):
        summ = None
        p = _summary_path(code, acc)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                summ = d.get("summary") if d.get("status") == "ok" else None
            except (OSError, json.JSONDecodeError):
                pass
        for ex, info in sorted((ent.get("exhibits") or {}).items()):
            items.append({"date": ent.get("date"), "accession": acc, "exhibit": ex, "description": info.get("description"),
                          "filename": info.get("filename"), "drive": info.get("drive"), "chars": info.get("chars"),
                          "summary": summ if ex == ("EX-99.1" if ent["exhibits"].get("EX-99.1") else "EX-99.2") else None})
    return {"market": "US", "code": code, "items": items,
            "ai": {"used": ai_budget.used("ir_release"), "limit": ai_budget.DEFAULT_LIMITS.get("ir_release")}}
