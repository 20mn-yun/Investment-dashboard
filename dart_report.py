import os
import re
import subprocess
import threading
import time
from datetime import date, timedelta
from uuid import uuid4

import requests
from dotenv import load_dotenv

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
            data = res.json()
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


def download_original(rcept_no, save_dir, filename_base):
    if not DART_API_KEY:
        return None

    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=30,
        )
    except Exception:
        return None

    if res.status_code != 200 or len(res.content) <= 100:
        return None

    orig_dir = os.path.join(save_dir, "원본")
    os.makedirs(orig_dir, exist_ok=True)

    safe_name = _safe_filename(filename_base) + ".zip"
    save_path = os.path.join(orig_dir, safe_name)

    with open(save_path, "wb") as f:
        f.write(res.content)

    time.sleep(0.3)
    return save_path


_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_DCM_NO_RE = re.compile(r"""dcmNo[^0-9]*?(\d{6,9})""")


def _extract_main_dcm_no(rcept_no):
    url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=15)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    m = _DCM_NO_RE.search(resp.text)
    return m.group(1) if m else None


def download_pdf(rcept_no, save_dir, filename_base):
    dcm_no = _extract_main_dcm_no(rcept_no)
    if not dcm_no:
        return None, None, None

    viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    headers = {"User-Agent": _BROWSER_UA, "Referer": viewer_url}
    params = {"rcp_no": rcept_no, "dcm_no": dcm_no}

    pdf_content = None
    used_endpoint = None

    for endpoint in [
        "https://dart.fss.or.kr/pdf/download/pdf.do",
        "https://dart.fss.or.kr/pdf/download/main.do",
    ]:
        try:
            resp = requests.get(endpoint, params=params, headers=headers, timeout=30)
        except Exception:
            time.sleep(0.5)
            continue
        if resp.content[:4] == b"%PDF":
            pdf_content = resp.content
            used_endpoint = endpoint.split("/")[-1]
            break
        else:
            print(f"  [{endpoint.split('/')[-1]}] not PDF: status={resp.status_code}, "
                  f"ct={resp.headers.get('content-type','')}, head={resp.content[:100]}")
        time.sleep(0.5)

    if not pdf_content:
        return dcm_no, None, None

    pdf_dir = os.path.join(save_dir, "PDF")
    os.makedirs(pdf_dir, exist_ok=True)
    safe_name = _safe_filename(filename_base) + ".pdf"
    save_path = os.path.join(pdf_dir, safe_name)
    with open(save_path, "wb") as f:
        f.write(pdf_content)

    return dcm_no, used_endpoint, save_path


_jobs = {}


def get_job(job_id):
    return _jobs.get(job_id)


def start_download_job(corp_code, corp_name, date_from, date_to,
                       download_base, want_xml=True, want_pdf=True):
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
    }
    t = threading.Thread(
        target=_run_download_job,
        args=(job_id, corp_code, corp_name, date_from, date_to,
              download_base, want_xml, want_pdf),
        daemon=True,
    )
    t.start()
    return job_id


def _run_download_job(job_id, corp_code, corp_name, date_from, date_to,
                      download_base, want_xml, want_pdf):
    job = _jobs[job_id]
    try:
        bgn = date_from.replace("-", "")
        end = date_to.replace("-", "")
        filings = list_periodic_filings(corp_code, bgn, end)
        job["found"] = len(filings)
        if not filings:
            job["status"] = "done"
            return

        final_dir = os.path.join(os.path.expanduser(download_base),
                                 corp_name, "사업보고서")
        staging_dir = os.path.join("downloads", "dart", corp_name, "사업보고서")
        os.makedirs(staging_dir, exist_ok=True)

        existing_xml = set()
        existing_pdf = set()
        xml_dir = os.path.join(final_dir, "원본")
        pdf_dir = os.path.join(final_dir, "PDF")
        if os.path.isdir(xml_dir):
            existing_xml = set(os.listdir(xml_dir))
        if os.path.isdir(pdf_dir):
            existing_pdf = set(os.listdir(pdf_dir))

        job["status"] = "downloading"

        for filing in filings:
            if job["stop_requested"]:
                job["status"] = "stopping"
                break

            rcept_no = filing["rcept_no"]
            fname_base = _safe_filename(
                f"{filing['corp_name']}_{filing['report_nm']}_{filing['rcept_dt']}"
            )

            xml_name = fname_base + ".zip"
            pdf_name = fname_base + ".pdf"

            xml_skip = xml_name in existing_xml
            pdf_skip = pdf_name in existing_pdf

            if (not want_xml or xml_skip) and (not want_pdf or pdf_skip):
                job["skipped"] += 1
                job["done"] += 1
                continue

            if want_xml and not xml_skip:
                path = download_original(rcept_no, staging_dir, fname_base)
                if path:
                    job["files"].append({
                        "filename": os.path.basename(path),
                        "report_nm": filing["report_nm"],
                        "rcept_dt": filing["rcept_dt"],
                        "kind": "xml",
                    })

            if want_pdf and not pdf_skip:
                _, _, path = download_pdf(rcept_no, staging_dir, fname_base)
                if path:
                    job["files"].append({
                        "filename": os.path.basename(path),
                        "report_nm": filing["report_nm"],
                        "rcept_dt": filing["rcept_dt"],
                        "kind": "pdf",
                    })

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
