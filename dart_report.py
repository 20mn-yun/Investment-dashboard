import io
import os
import re
import subprocess
import threading
import time
import zipfile
from datetime import date, timedelta
from uuid import uuid4

import requests
from dotenv import load_dotenv

import telegram_report


class DartPausedError(RuntimeError):
    pass


def _check_paused():
    import kr_financials
    p = kr_financials.dart_is_paused()
    if p:
        raise DartPausedError(f"DART 점검 중, {p}까지 대기")


def _mark_html(where, res):
    import kr_financials
    return kr_financials.dart_mark_html_response(f"{where} {(res.headers.get('Content-Type') or '?').split(';')[0]} {len(res.content)}B")


def _is_html(res):
    ct = (res.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return ct.startswith("text/html") or (res.content or b"")[:64].lstrip().lower().startswith(b"<!doctype html") or (res.content or b"")[:6].lstrip().lower().startswith(b"<html")

load_dotenv()

DART_API_KEY = os.environ.get("DART_API_KEY")

PERIODIC_KEYWORDS = ["분기보고서", "반기보고서", "사업보고서"]

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')


def _safe_filename(name):
    return _UNSAFE_CHARS.sub("_", name)


def list_periodic_filings(corp_code, bgn_de, end_de):
    if not DART_API_KEY:
        return []

    all_items = []
    page_no = 1

    while True:
        _check_paused()
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "corp_code": corp_code,
                    "bgn_de": bgn_de,
                    "end_de": end_de,
                    "pblntf_ty": "A",
                    "page_count": 100,
                    "page_no": page_no,
                },
                timeout=15,
            )
            if _is_html(res) or "json" not in (res.headers.get("Content-Type") or "").lower():
                raise DartPausedError(_mark_html("list.json", res))
            data = res.json()
        except DartPausedError:
            raise
        except Exception:
            break

        if data.get("status") != "000":
            break

        items = data.get("list", [])
        all_items.extend(items)

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break
        page_no += 1

    results = []
    for item in all_items:
        report_nm = item.get("report_nm", "")
        if not any(kw in report_nm for kw in PERIODIC_KEYWORDS):
            continue
        results.append({
            "rcept_no": item.get("rcept_no", ""),
            "report_nm": report_nm,
            "rcept_dt": item.get("rcept_dt", ""),
            "corp_name": item.get("corp_name", ""),
        })

    return results


def _save_zip_members(data, orig_dir, report_name, rcept_no=None):
    """zip 바이트를 풀어 <orig_dir>/<보고서명>.xml(본문) + <보고서명>_첨부n.<ext>(나머지) 로 저장 (4-G).
    본문 = rcept_no 와 같은 이름의 항목, 없으면 가장 큰 xml. 반환 저장한 파일명 목록"""
    saved = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        if not members:
            return saved
        main = next((i for i in members if rcept_no and os.path.splitext(os.path.basename(i.filename))[0] == str(rcept_no)), None)
        if main is None:
            xmls = [i for i in members if i.filename.lower().endswith(".xml")] or members
            main = max(xmls, key=lambda i: i.file_size)
        os.makedirs(orig_dir, exist_ok=True)
        n = 0
        for info in sorted(members, key=lambda i: (i is not main, i.filename)):
            ext = os.path.splitext(info.filename)[1] or ".xml"
            if info is main:
                name = f"{report_name}{ext}"
            else:
                n += 1
                name = f"{report_name}_첨부{n}{ext}"
            with zf.open(info) as src, open(os.path.join(orig_dir, name), "wb") as dst:
                dst.write(src.read())
            saved.append(name)
    return saved


def download_original(rcept_no, save_dir, filename_base):
    """document.xml(zip) 을 받아 메모리에서 풀어 <save_dir>/원본/<보고서명>.xml (+첨부) 로 저장 (zip 은 저장하지 않음, 4-G).
    반환 (본문 경로 또는 None, 실패 사유). HTML 응답만 차단기 표시, 그 외 비정상은 사유만 남기고 계속."""
    if not DART_API_KEY:
        return None, "DART 키 없음"
    _check_paused()
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=30,
        )
    except Exception as e:
        return None, f"요청 실패 {type(e).__name__}"

    if _is_html(res):
        raise DartPausedError(_mark_html("document.xml", res))
    if res.status_code != 200 or len(res.content) <= 100:
        return None, f"HTTP {res.status_code}, {len(res.content)}B"
    if res.content[:2] != b"PK":
        return None, f"ZIP 아님 ({(res.headers.get('Content-Type') or '?').split(';')[0]}, {len(res.content)}B)"

    orig_dir = os.path.join(save_dir, "원본")
    try:
        saved = _save_zip_members(res.content, orig_dir, _safe_filename(filename_base), rcept_no)
    except zipfile.BadZipFile:
        return None, "ZIP 손상"
    time.sleep(0.3)
    if not saved:
        return None, "zip 안이 비어 있음"
    return os.path.join(orig_dir, saved[0]), None


def reorganize_report_dir(final_dir):
    """4-G 1회 정리: <사업보고서>/ 바로 아래 PDF → PDF/, <보고서명>_원본/ 안 파일 → 원본/<보고서명>.xml(+첨부), 빈 _원본/ 삭제,
    원본/*.zip 은 그대로 두고 목록만. 반환 집계"""
    res = {"pdf_moved": 0, "orig_moved": 0, "attach_named": 0, "dirs_removed": 0, "skipped": [], "zips": []}
    if not os.path.isdir(final_dir):
        return res
    pdf_dir, orig_dir = os.path.join(final_dir, "PDF"), os.path.join(final_dir, "원본")
    for fn in sorted(os.listdir(final_dir)):
        p = os.path.join(final_dir, fn)
        if os.path.isfile(p) and fn.lower().endswith(".pdf"):
            os.makedirs(pdf_dir, exist_ok=True)
            dst = os.path.join(pdf_dir, fn)
            if os.path.exists(dst):
                res["skipped"].append(fn)
                continue
            os.rename(p, dst)
            res["pdf_moved"] += 1
        elif os.path.isdir(p) and fn.endswith("_원본"):
            report_name = fn[:-3]
            files = [x for x in sorted(os.listdir(p)) if os.path.isfile(os.path.join(p, x))]
            if not files:
                os.rmdir(p); res["dirs_removed"] += 1
                continue
            main = next((x for x in files if re.fullmatch(r"\d{14}\.xml", x)), None) or \
                   max([x for x in files if x.lower().endswith(".xml")] or files, key=lambda x: os.path.getsize(os.path.join(p, x)))
            os.makedirs(orig_dir, exist_ok=True)
            n = 0
            ok = True
            for x in sorted(files, key=lambda x: (x != main, x)):
                ext = os.path.splitext(x)[1] or ".xml"
                if x == main:
                    name = f"{report_name}{ext}"
                else:
                    n += 1
                    name = f"{report_name}_첨부{n}{ext}"
                dst = os.path.join(orig_dir, name)
                if os.path.exists(dst):
                    res["skipped"].append(f"{fn}/{x}")
                    ok = False
                    continue
                os.rename(os.path.join(p, x), dst)
                res["orig_moved"] += 1
                if x != main:
                    res["attach_named"] += 1
            if ok and not os.listdir(p):
                os.rmdir(p); res["dirs_removed"] += 1
    if os.path.isdir(orig_dir):
        res["zips"] = sorted(x for x in os.listdir(orig_dir) if x.lower().endswith(".zip"))
    return res


_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_DCM_NO_RE = re.compile(r"""dcmNo[^0-9]*?(\d{6,9})""")


def _viewer_session():
    s = requests.Session()
    s.headers.update({"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def _extract_main_dcm_no(rcept_no, session=None):
    url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    try:
        resp = (session or requests).get(url, headers={"User-Agent": _BROWSER_UA}, timeout=15)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    m = _DCM_NO_RE.search(resp.text)
    return m.group(1) if m else None


def download_pdf(rcept_no, save_dir, filename_base):
    """DART 뷰어의 PDF 받기 (4-F): 뷰어 페이지(쿠키·dcm_no) → 다운로드 대화상자(pdf/download/main.do) → 그 주소를 Referer 로 pdf.do.
    세션·Referer 없이 pdf.do 를 바로 부르면 빈 응답(0B)이 온다. 반환 (dcm_no, 쓴 엔드포인트, 저장 경로, 실패 사유).
    PDF 는 %PDF 매직/application/pdf 로만 판정하고, 뷰어(dart.fss.or.kr)의 HTML 응답은 차단기 사유로 쓰지 않는다."""
    _check_paused()                      # 쉬는 중에만 건너뜀
    sess = _viewer_session()
    dcm_no = _extract_main_dcm_no(rcept_no, sess)
    if not dcm_no:
        return None, None, None, "뷰어 페이지에서 dcm_no 를 찾지 못함"

    dialog_url = f"https://dart.fss.or.kr/pdf/download/main.do?rcp_no={rcept_no}&dcm_no={dcm_no}"
    reasons = []
    try:
        sess.get(dialog_url, timeout=20)          # 다운로드 대화상자 (세션 준비)
    except Exception as e:
        reasons.append(f"main.do: 요청 실패 {type(e).__name__}")

    pdf_content = None
    used_endpoint = None
    try:
        resp = sess.get("https://dart.fss.or.kr/pdf/download/pdf.do", params={"rcp_no": rcept_no, "dcm_no": dcm_no},
                        headers={"Referer": dialog_url}, timeout=60)
        if resp.content[:4] == b"%PDF" or (resp.headers.get("content-type") or "").lower().startswith("application/pdf"):
            pdf_content, used_endpoint = resp.content, "pdf.do"
        else:
            ct = (resp.headers.get("content-type") or "").split(";")[0] or "없음"
            kind = "HTML 페이지" if _is_html(resp) else ("빈 응답" if not resp.content else f"{ct} {len(resp.content)}B")
            reasons.append(f"pdf.do: PDF 아님 (HTTP {resp.status_code}, {kind})")
    except Exception as e:
        reasons.append(f"pdf.do: 요청 실패 {type(e).__name__}")
    time.sleep(0.5)

    if not pdf_content:
        return dcm_no, None, None, "; ".join(reasons) or "PDF 없음"

    pdf_dir = os.path.join(save_dir, "PDF")
    os.makedirs(pdf_dir, exist_ok=True)
    safe_name = _safe_filename(filename_base) + ".pdf"
    save_path = os.path.join(pdf_dir, safe_name)           # <사업보고서>/PDF/ (4-G)
    with open(save_path, "wb") as f:
        f.write(pdf_content)

    return dcm_no, used_endpoint, save_path, None


_jobs = {}


def get_job(job_id):
    return _jobs.get(job_id)


def report_dir_for(download_base, corp_name, market="KR", code=None):
    """저장 폴더: <stock_folder(종목)>/사업보고서/ (4-E, 종목 폴더 키워드 공통 함수). code 가 없으면 corp_name 그대로 (이전과 동일)"""
    kw = telegram_report.stock_folder_keyword(market, code, corp_name) if code else corp_name
    return os.path.join(telegram_report.stock_folder(download_base, kw), "사업보고서")


def start_download_job(corp_code, corp_name, date_from, date_to,
                       download_base, want_xml=True, want_pdf=True, market="KR", code=None):
    job_id = uuid4().hex[:8]
    _jobs[job_id] = {
        "status": "searching",
        "found": 0,
        "done": 0,
        "files": [],
        "skipped": 0,
        "download_path": "",
        "error": None,
        "stop_requested": False,
        "pdf_ok": 0, "pdf_failed": [], "xml_ok": 0, "xml_failed": [],
    }
    t = threading.Thread(
        target=_run_download_job,
        args=(job_id, corp_code, corp_name, date_from, date_to,
              download_base, want_xml, want_pdf, market, code),
        daemon=True,
    )
    t.start()
    return job_id


def _run_download_job(job_id, corp_code, corp_name, date_from, date_to,
                      download_base, want_xml, want_pdf, market="KR", code=None):
    job = _jobs[job_id]
    try:
        bgn = date_from.replace("-", "")
        end = date_to.replace("-", "")
        filings = list_periodic_filings(corp_code, bgn, end)
        job["found"] = len(filings)
        if not filings:
            job["status"] = "done"
            return

        final_dir = report_dir_for(download_base, corp_name, market, code)
        staging_dir = os.path.join("downloads", "dart", corp_name, "사업보고서")
        os.makedirs(staging_dir, exist_ok=True)

        existing = set()                       # 새 위치(PDF/*.pdf, 원본/*.xml) + 옛 위치(바로 아래 PDF, <보고서명>_원본/, 원본/*.zip)
        if os.path.isdir(final_dir):
            existing |= set(os.listdir(final_dir))
            for sub in ("원본", "PDF"):
                sp = os.path.join(final_dir, sub)
                if os.path.isdir(sp):
                    existing |= set(os.listdir(sp))

        job["status"] = "downloading"

        for filing in filings:
            if job["stop_requested"]:
                job["status"] = "stopping"
                break

            rcept_no = filing["rcept_no"]
            fname_base = _safe_filename(
                f"{filing['corp_name']}_{filing['report_nm']}_{filing['rcept_dt']}"
            )

            xml_skip = any(k in existing for k in (fname_base + ".xml", fname_base + "_원본", fname_base + ".zip"))
            pdf_skip = (fname_base + ".pdf") in existing

            if (not want_xml or xml_skip) and (not want_pdf or pdf_skip):
                job["skipped"] += 1
                job["done"] += 1
                continue

            if want_xml and not xml_skip:
                path, why = download_original(rcept_no, staging_dir, fname_base)
                if path:
                    job["xml_ok"] += 1
                    job["files"].append({"filename": "원본/" + os.path.basename(path), "report_nm": filing["report_nm"],
                                         "rcept_dt": filing["rcept_dt"], "kind": "원본", "note": ""})
                else:
                    job["xml_failed"].append({"filename": fname_base, "reason": why})
                    job["files"].append({"filename": "원본/" + fname_base + ".xml", "report_nm": filing["report_nm"],
                                         "rcept_dt": filing["rcept_dt"], "kind": "원본 실패", "note": why})

            if want_pdf and not pdf_skip:
                _, _, path, why = download_pdf(rcept_no, staging_dir, fname_base)
                if path:
                    job["pdf_ok"] += 1
                    job["files"].append({"filename": "PDF/" + os.path.basename(path), "report_nm": filing["report_nm"],
                                         "rcept_dt": filing["rcept_dt"], "kind": "pdf", "note": ""})
                else:                           # 한 건 실패는 사유만 남기고 다음 보고서로 (DART 점검 중은 예외로 즉시 중단)
                    job["pdf_failed"].append({"filename": fname_base + ".pdf", "reason": why})
                    job["files"].append({"filename": fname_base + ".pdf", "report_nm": filing["report_nm"],
                                         "rcept_dt": filing["rcept_dt"], "kind": "pdf 실패", "note": why})

            job["done"] += 1

        job["status"] = "copying"
        r_mkdir = subprocess.run(
            ["/bin/mkdir", "-p", final_dir], capture_output=True, text=True)
        if r_mkdir.returncode != 0:
            raise RuntimeError(f"mkdir 실패: {r_mkdir.stderr}")

        r_cp = subprocess.run(
            ["/bin/cp", "-R", staging_dir + "/.", final_dir + "/"],
            capture_output=True, text=True)
        if r_cp.returncode != 0:
            raise RuntimeError(
                f"cp 실패: {r_cp.stderr} (staging 보존: {staging_dir})")

        subprocess.run(["/bin/rm", "-rf", staging_dir], capture_output=True)
        parent = os.path.dirname(staging_dir)
        if os.path.isdir(parent) and not os.listdir(parent):
            subprocess.run(["/bin/rm", "-rf", parent], capture_output=True)

        job["download_path"] = final_dir
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from earnings_tracker import get_corp_code

    corp_code = get_corp_code("005930")
    print(f"corp_code: 005930 → {corp_code}")
    if not corp_code:
        sys.exit(1)

    today = date.today().isoformat()
    one_year_ago = (date.today() - timedelta(days=365)).isoformat()

    job_id = start_download_job(
        corp_code, "삼성전자", one_year_ago, today,
        download_base="./_dart_test_drive",
        want_xml=True, want_pdf=True,
    )
    print(f"job_id: {job_id}")

    while True:
        j = get_job(job_id)
        print(f"  status={j['status']}  found={j['found']}  done={j['done']}  skipped={j['skipped']}  files={len(j['files'])}")
        if j["status"] in ("done", "error"):
            break
        time.sleep(2)

    print(f"\n=== 최종 job 상태 ===")
    import json
    print(json.dumps(j, ensure_ascii=False, indent=2))

    dl_path = j.get("download_path", "")
    if dl_path:
        for sub in ["원본", "PDF"]:
            d = os.path.join(dl_path, sub)
            if os.path.isdir(d):
                files = os.listdir(d)
                print(f"\n{d}: {len(files)}개")
                for fn in sorted(files):
                    print(f"  {fn}")
            else:
                print(f"\n{d}: 폴더 없음")
