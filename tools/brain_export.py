"""대시보드가 만든 종목 자료를 gbrain 지식창고(~/investment_brain)용 마크다운으로 내보낸다 (brain export v1).

대상 = watchlist.load_watchlist() 전부. 변환은 기존 함수만 호출한다(기존 모듈 수정 없음):
  sources/briefs/<kw>.md            ← cache/briefs/<MARKET>_<code>.md 그대로
  sources/calls/<kw>/<Q>.md         ← earnings_calls._summary_md (+ _compare_md 를 '## 직전 분기 비교' 로)   (미국만)
  sources/reports/<kw>.md           ← stock_reports._render_summary_md
  sources/dart/<kw>/<dt>_<no>.md    ← dart_daily_history.json (워치리스트 KR, summary 있는 것)
  analysis/<kw>.md                  ← stock_notes.render_markdown(get_note)
  companies/<kw>.md                 ← 종목 한 장(개요·나의 판단·See Also·Timeline)
kw = telegram_report.stock_folder_keyword(market, code, name). 같은 입력이면 같은 출력(멱등), 쓰기는 tmp → os.replace.
실행: /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 tools/brain_export.py [--dry-run] [--out DIR]
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import unicodedata

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DASH not in sys.path:
    sys.path.insert(0, DASH)
os.chdir(DASH)

import earnings_calls      # noqa: E402
import stock_notes         # noqa: E402
import stock_reports       # noqa: E402
import telegram_report     # noqa: E402
import watchlist           # noqa: E402

DEFAULT_OUT = os.path.expanduser("~/investment_brain")
DART_HISTORY = os.path.join(DASH, "dart_daily_history.json")


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _fm(title, aliases):
    """frontmatter: title·aliases 만 (값은 JSON 문자열 = 유효한 YAML)"""
    al = []
    for a in aliases:
        a = nfc(str(a or "")).strip()
        if a and a not in al:
            al.append(a)
    return "---\ntitle: " + json.dumps(nfc(title), ensure_ascii=False) + "\naliases: [" + ", ".join(json.dumps(a, ensure_ascii=False) for a in al) + "]\n---\n\n"


class Writer:
    def __init__(self, out, dry):
        self.out, self.dry = out, dry
        self.counts, self.changed = {}, 0

    def write(self, kind, rel, text):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if self.dry:
            return
        path = os.path.join(self.out, nfc(rel))
        text = nfc(text if text.endswith("\n") else text + "\n")
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read() == text:
                    return
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        self.changed += 1


def _brief_overview(text):
    """브리프의 '## 최근 실적' 전까지(첫 H1 제목 줄 제외)를 > 인용으로"""
    head = text.split("\n## 최근 실적", 1)[0]
    lines = head.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    lines = [l for l in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(("> " + l) if l.strip() else ">" for l in lines) or "> 브리프 없음"


def export(out, dry):
    t0 = time.time()
    w = Writer(out, dry)
    wl = watchlist.load_watchlist()
    notes = {(n["market"], n["code"]) for n in stock_notes.list_notes()}
    hist = (_load_json(DART_HISTORY, {}) or {}).get("days") or {}
    kr_codes = {i["code"] for i in wl if i["market"] == "KR"}
    dart_by_code = {}
    seen_rcept = set()
    for day in sorted(hist):
        for it in hist[day] or []:
            code, no = it.get("stock_code"), it.get("rcept_no")
            if code in kr_codes and (it.get("summary") or "").strip() and no and no not in seen_rcept:
                seen_rcept.add(no)
                dart_by_code.setdefault(code, []).append(it)
    failures = []
    for item in wl:
        market, code, name = item["market"], item["code"], nfc(item.get("name") or item["code"])
        try:
            kw = nfc(telegram_report.stock_folder_keyword(market, code, name))
        except Exception as e:
            failures.append(f"{market}:{code} kw {type(e).__name__}")
            continue
        aliases = [name, code] + ([item["name_kr"]] if item.get("name_kr") else [])
        links, timeline = [], []

        # 1) 브리프
        brief_path = os.path.join(DASH, "cache", "briefs", f"{market}_{code}.md")
        brief = None
        if os.path.exists(brief_path):
            with open(brief_path, "r", encoding="utf-8") as f:
                brief = f.read()
            w.write("briefs", f"sources/briefs/{kw}.md", _fm(f"{name} 브리프", aliases) + brief)
            links.append(f"[[sources/briefs/{kw}]]")

        # 3) 리포트
        state = _load_json(stock_reports._state_path(market, code))
        summaries = _load_json(stock_reports._sum_path(market, code), {}) or {}
        if state and state.get("items"):
            try:
                items = list(state["items"].values())
                md = stock_reports._render_summary_md(name, code, items, summaries)
                md = re.sub(r"^- 갱신: .*$", f"- 갱신: {state.get('updated_at') or '-'}", md, count=1, flags=re.M)   # 멱등: 현재 시각 대신 저장본 갱신 시각
                w.write("reports", f"sources/reports/{kw}.md", _fm(f"{name} 리포트 요약", aliases) + md)
                links.append(f"[[sources/reports/{kw}]]")
                for it in items:
                    s = summaries.get(str(it.get("message_id"))) or {}
                    if s.get("status") in ("duplicate", "irrelevant"):
                        continue
                    d = (it.get("date") or "")[:10]
                    if not re.match(r"\d{4}-\d{2}-\d{2}$", d):
                        continue
                    title = re.sub(r"\.pdf$", "", it.get("filename") or "", flags=re.I)
                    tgt = f" (목표주가 {int(s['target']):,})" if s.get("status") == "ok" and s.get("target") else ""
                    timeline.append((d, f"리포트 — {s.get('broker') or it.get('broker') or '-'} {title}{tgt}"))
            except Exception as e:
                failures.append(f"{market}:{code} reports {type(e).__name__}: {str(e)[:60]}")

        # 2) 컨콜 (미국)
        call_links = []
        if market == "US":
            for fp in sorted(glob.glob(os.path.join(DASH, "cache", "calls", f"US_{code}", "*_summary.json"))):
                q = os.path.basename(fp)[:-len("_summary.json")]
                summ = _load_json(fp)
                if not summ or summ.get("status") != "ok":
                    continue
                try:
                    md = earnings_calls._summary_md(code, q, name, summ)
                    cmp_files = sorted(glob.glob(os.path.join(DASH, "cache", "calls", f"US_{code}", f"{q}_vs_*_compare.json")))
                    for cf in cmp_files[-1:]:
                        res = _load_json(cf)
                        if res and res.get("status") == "ok" and res.get("compare"):
                            prev = os.path.basename(cf)[len(q) + 4:-len("_compare.json")]
                            cmd = earnings_calls._compare_md(code, q, prev, name, res)
                            md += "\n## 직전 분기 비교\n\n" + re.sub(r"^# ", "### ", cmd, count=1)
                            w.counts["compares"] = w.counts.get("compares", 0) + 1
                    w.write("calls", f"sources/calls/{kw}/{q}.md", _fm(f"{name} {q} 컨콜", aliases) + md)
                    call_links.append(q)
                    d = str(summ.get("at") or "")[:10]
                    if re.match(r"\d{4}-\d{2}-\d{2}$", d):
                        timeline.append((d, f"컨콜 — {q} 요약 [[sources/calls/{kw}/{q}]]"))
                except Exception as e:
                    failures.append(f"US:{code} call {q} {type(e).__name__}: {str(e)[:60]}")

        # 4) 공시 (한국)
        for it in dart_by_code.get(code, []) if market == "KR" else []:
            dt, no = str(it.get("rcept_dt") or ""), str(it["rcept_no"])
            corp, title = nfc(it.get("corp_name") or name), nfc(it.get("title") or "")
            body = f"# {corp} · {title}\n\n{nfc(it['summary']).strip()}\n\nrcept_no: {no}\n"
            w.write("dart", f"sources/dart/{kw}/{dt}_{no}.md", _fm(f"{corp} · {title}", aliases) + body)
            if re.match(r"\d{8}$", dt):
                timeline.append((f"{dt[:4]}-{dt[4:6]}-{dt[6:]}", f"공시 — {title} [[sources/dart/{kw}/{dt}_{no}]]"))

        # 5) 나의 판단
        judge = "(없음)"
        if (market, code) in notes:
            note = stock_notes.get_note(market, code, name)
            w.write("analysis", f"analysis/{kw}.md", _fm(f"{name} 나의 판단", aliases) + stock_notes.render_markdown(note))
            links.append(f"[[analysis/{kw}]]")
            judge = "\n\n".join(f"### {stock_notes.FIELD_LABELS[k]}\n\n{(note['fields'].get(k) or '').strip() or '(비어 있음)'}" for k in stock_notes.FIELD_KEYS)
            for h in note.get("history") or []:
                d = str(h.get("at") or "")[:10]
                if re.match(r"\d{4}-\d{2}-\d{2}$", d):
                    after = re.sub(r"\s+", " ", (h.get("after") or "").strip())[:80]
                    timeline.append((d, f"판단 — {h.get('label') or h.get('field')}: {after}"))

        # 6) 종목 한 장
        see = " · ".join(links + ([("컨콜 " + " · ".join(f"[[sources/calls/{kw}/{q}]]" for q in sorted(call_links, reverse=True)))] if call_links else []))
        timeline.sort(key=lambda x: (x[0], x[1]), reverse=True)
        lines = [f"# {name} ({market}:{code})", "", _brief_overview(brief) if brief else "> 브리프 없음", "", "## 나의 판단", "", judge, "",
                 "## See Also", "", f"- {see}" if see else "- (없음)", "", "---", "", "## Timeline", ""]
        lines += [f"- **{d}** | {t}" for d, t in timeline] or ["- (없음)"]
        w.write("companies", f"companies/{kw}.md", _fm(name, [code] + ([item["name_kr"]] if item.get("name_kr") else [])) + "\n".join(lines))
    return w, failures, round(time.time() - t0, 2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="대시보드 자료 → gbrain 마크다운")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 건수만")
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    w, fails, secs = export(os.path.expanduser(a.out), a.dry_run)
    print(json.dumps({"out": a.out, "dry_run": a.dry_run, "counts": w.counts, "changed_files": w.changed, "seconds": secs, "failures": fails}, ensure_ascii=False, indent=1))
