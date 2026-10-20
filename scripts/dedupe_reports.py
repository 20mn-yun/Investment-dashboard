#!/usr/bin/env python3
"""리포트 중복 백필·정리 (일회성). 워치리스트 전 종목의 <종목 폴더>/리포트/ PDF 를 내용 기준으로 묶는다.

  python3 scripts/dedupe_reports.py            # --dry-run: 종목별 [keeper] / [여분 → 이동 예정] 와 집계만 출력, 아무것도 바꾸지 않음
  python3 scripts/dedupe_reports.py --apply    # index 보강 + 여분을 리포트/_중복/ 으로 이동(삭제 없음) + summaries.json 에 duplicate 기록 + 로그

중복 그룹 = sha256 이 같거나 text_fp(페이지수|첫 페이지 텍스트) 가 같은 파일 묶음.
keeper = 새 규칙 파일명(<종목>_<yymmdd>_ 접두) 우선 → 그중 index saved_at 이 가장 빠른 것(없으면 mtime 이 가장 빠른 것).
"""
import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_report as tr          # noqa: E402
import stock_reports                  # noqa: E402
import watchlist as watchlist_store   # noqa: E402

nfc = lambda s: unicodedata.normalize("NFC", s) if isinstance(s, str) else s
DUP_SUBFOLDER = "_중복"
NEW_RULE_RE = re.compile(r"^.+?_\d{6}_")      # <종목>_<yymmdd>_ 접두


def _is_new_rule(fn):
    return bool(NEW_RULE_RE.match(fn or ""))


def scan_stock(it):
    market, code, name = it["market"], it["code"], it.get("name", "")
    root = tr.stock_folder(tr.report_download_base(), tr.stock_folder_keyword(market, code, name or None))
    d = tr.report_dir(root)
    if not os.path.isdir(d):
        return None
    idx = tr.load_report_index(root)
    by_fn = {nfc(e.get("filename")): (k, e) for k, e in idx.items() if e.get("filename")}
    files = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".pdf") or fn.startswith("."):
            continue
        p = os.path.join(d, fn)
        fp = tr.pdf_fingerprint(p)
        key, ent = by_fn.get(nfc(fn), (None, None))
        files.append({"fn": nfc(fn), "path": p, "key": key, "entry": ent, "fp": fp,
                      "saved_at": (ent or {}).get("saved_at") or datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds"),
                      "mtime": os.path.getmtime(p)})
    # 그룹: sha256 또는 text_fp 가 같으면 연결 (union-find)
    parent = {i: i for i in range(len(files))}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        parent[find(a)] = find(b)

    for key in ("sha256", "text_fp"):
        seen = {}
        for i, f in enumerate(files):
            v = f["fp"].get(key)
            if not v:
                continue
            if v in seen:
                union(i, seen[v])
            else:
                seen[v] = i
    groups = {}
    for i in range(len(files)):
        groups.setdefault(find(i), []).append(files[i])
    dup_groups = []
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda f: (0 if _is_new_rule(f["fn"]) else 1, f["saved_at"], f["mtime"]))
        keeper, extras = g[0], g[1:]
        why = "sha256" if all(f["fp"].get("sha256") == keeper["fp"].get("sha256") for f in extras) else "text_fp"
        dup_groups.append({"keeper": keeper, "extras": extras, "why": why})
    return {"market": market, "code": code, "name": name, "root": root, "dir": d, "idx": idx, "files": files, "dup_groups": dup_groups}


def apply_stock(res, log):
    """index 보강 + 여분 이동 + summaries duplicate 기록"""
    raw = tr.load_report_index_raw(res["root"])
    for f in res["files"]:
        key = f["key"] or f"file:{(f['fp'].get('sha256') or '')[:12]}"
        old = raw.get(key)
        ent = dict(old) if isinstance(old, dict) else {"filename": f["fn"]}
        ent.update(filename=ent.get("filename") or f["fn"], sha256=f["fp"].get("sha256"), size=f["fp"].get("size"),
                   text_fp=f["fp"].get("text_fp"), pages=f["fp"].get("pages"))
        ent.setdefault("channel", None)
        ent.setdefault("original_name", None)
        ent.setdefault("saved_at", f["saved_at"])
        raw[key] = ent
        f["key"] = key
    tr.save_report_index(res["root"], raw)
    moved = 0
    dup_dir = os.path.join(res["dir"], DUP_SUBFOLDER)
    sum_path = stock_reports._sum_path(res["market"], res["code"])
    summaries = stock_reports._load_json(sum_path, {})
    changed = False
    for g in res["dup_groups"]:
        keeper = g["keeper"]
        for f in g["extras"]:
            os.makedirs(dup_dir, exist_ok=True)
            dst = os.path.join(dup_dir, f["fn"])
            if os.path.exists(f["path"]):
                shutil.move(f["path"], dst)
                moved += 1
            log.write(f"{datetime.now().isoformat(timespec='seconds')}\t{res['name']}\t{f['fn']}\t→ _중복/\tkeeper={keeper['fn']}\t{g['why']}\n")
            mid = f["key"] if f["key"] and not str(f["key"]).startswith("file:") else None
            if mid and mid in summaries and summaries[mid].get("status") != "duplicate":
                summaries[mid] = dict(summaries[mid], status="duplicate", duplicate_of=keeper["fn"], duplicate_why=g["why"], summarized_at=stock_reports._now())
                changed = True
            elif mid and mid not in summaries:
                summaries[mid] = {"message_id": int(mid) if str(mid).isdigit() else mid, "filename": f["fn"], "status": "duplicate",
                                  "duplicate_of": keeper["fn"], "duplicate_why": g["why"], "summarized_at": stock_reports._now()}
                changed = True
    if changed:
        stock_reports._save_json(sum_path, summaries)
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="index 보강 + 여분 이동 + summaries 기록 (기본은 dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="기본값. 아무것도 바꾸지 않고 목록·집계만 출력")
    ap.add_argument("--only", help="종목 코드 하나만")
    a = ap.parse_args()
    if a.apply and a.dry_run:
        ap.error("--apply 와 --dry-run 은 함께 쓸 수 없음")
    items = watchlist_store.load_watchlist()
    if a.only:
        items = [i for i in items if i["code"] == a.only]
    total_groups = total_extra = total_files = 0
    log = None
    if a.apply:
        os.makedirs("cache", exist_ok=True)
        log = open(os.path.join("cache", f"dedupe_reports_{datetime.now().strftime('%y%m%d')}.log"), "a", encoding="utf-8")
    print(f"모드: {'APPLY' if a.apply else 'DRY-RUN'} · 종목 {len(items)}개")
    for it in items:
        res = scan_stock(it)
        if res is None:
            print(f"\n## {it.get('name') or it['code']} ({it['market']}:{it['code']}): 리포트/ 폴더 없음")
            continue
        n_idx = sum(1 for f in res["files"] if f["key"])
        print(f"\n## {res['name'] or res['code']} ({res['market']}:{res['code']}): PDF {len(res['files'])}건 (index 등록 {n_idx}, 미등록 {len(res['files']) - n_idx}), 중복 그룹 {len(res['dup_groups'])}")
        for g in res["dup_groups"]:
            print(f"  [keeper] {g['keeper']['fn']}")
            for f in g["extras"]:
                print(f"    [여분 → 이동 예정] {f['fn']}  ({g['why']})")
        total_groups += len(res["dup_groups"])
        total_extra += sum(len(g["extras"]) for g in res["dup_groups"])
        total_files += len(res["files"])
        if a.apply:
            moved = apply_stock(res, log)
            print(f"  → index 보강 {len(res['files'])}건, 이동 {moved}건")
    print(f"\n합계: PDF {total_files}건, 중복 그룹 {total_groups}, 이동 {'예정' if not a.apply else '완료'} {total_extra}건")
    if log:
        log.close()
        print(f"로그: {log.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
