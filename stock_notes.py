"""나의 판단 저장소 — 종목별 판단 네 칸·메모·변경 이력. AI 호출 없음.

원본: stock_notes/<market>_<code>.json (공개 저장소이므로 .gitignore 대상)
Drive 내보내기: Analysis/종목판단/<종목명>_<코드>.md 와 종목판단_인덱스.md
  - 경로는 telegram_report.drive_path 로만 만든다. Drive 를 못 찾으면 원본 저장은 성공, 내보내기 실패만 알린다.
덮어쓰기 방지: 저장 요청의 version 이 서버 version 과 다르면 저장하지 않고 최신 내용을 돌려준다.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime

import telegram_report
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(_BD, "stock_notes")
EXPORT_SUBDIR = ("Analysis", "종목판단")
INDEX_NAME = "종목판단_인덱스.md"

FIELDS = [("idea", "투자아이디어"), ("assumptions", "핵심가정"), ("followup", "Follow-up 포인트"), ("risks", "Risk 포인트")]
FIELD_KEYS = [k for k, _ in FIELDS]
FIELD_LABELS = dict(FIELDS)
MEMO_SOURCES = ("대시보드",)

_locks = {}
_locks_guard = threading.Lock()
_index_lock = threading.Lock()


class Conflict(Exception):
    def __init__(self, note):
        super().__init__("다른 곳에서 먼저 수정됨")
        self.note = note


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _key(market, code):
    market = (market or "").strip().upper()
    if market not in watchlist_store.MARKETS:
        raise ValueError("market 은 KR 또는 US")
    c = watchlist_store.normalize_code(market, code)
    if not c:
        raise ValueError("code 형식 오류")
    return market, c


def _path(market, code):
    return os.path.join(NOTES_DIR, f"{market}_{code}.json")


def _empty(market, code, name=""):
    return {"market": market, "code": code, "name": name or "",
            "fields": {k: "" for k in FIELD_KEYS}, "version": 0,
            "created_at": None, "updated_at": None, "history": [], "memos": []}


def _read(market, code):
    try:
        with open(_path(market, code), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    d.setdefault("fields", {})
    for k in FIELD_KEYS:
        d["fields"].setdefault(k, "")
    d.setdefault("history", [])
    d.setdefault("memos", [])
    d.setdefault("version", 0)
    return d


def _write(note):
    os.makedirs(NOTES_DIR, exist_ok=True)
    watchlist_store.atomic_write_json(_path(note["market"], note["code"]), note)


def has_content(note):
    return bool(note) and (any((note["fields"].get(k) or "").strip() for k in FIELD_KEYS) or bool(note["memos"]))


def public(note):
    """API 응답용 (메모·이력은 최신순)"""
    out = dict(note)
    out["memos"] = sorted(note["memos"], key=lambda m: m.get("at", ""), reverse=True)
    out["history"] = sorted(note["history"], key=lambda h: h.get("at", ""), reverse=True)
    out["field_labels"] = FIELD_LABELS
    out["has_content"] = has_content(note)
    return out


# ===== 조회·변경 =====
def get_note(market, code, name=""):
    market, code = _key(market, code)
    with _lock((market, code)):
        note = _read(market, code) or _empty(market, code, name)
        if name and not note.get("name"):
            note["name"] = name
        return note


def save_fields(market, code, fields, version, name=""):
    """네 칸 저장. 반환: (note, changed_fields, export_result). version 불일치면 Conflict."""
    market, code = _key(market, code)
    with _lock((market, code)):
        note = _read(market, code) or _empty(market, code, name)
        if int(version) != int(note["version"]):
            raise Conflict(note)
        now = _now()
        changed = []
        for k in FIELD_KEYS:
            if k not in fields:
                continue
            new = str(fields.get(k) or "").replace("\r\n", "\n").strip()
            old = (note["fields"].get(k) or "")
            if new != old:
                note["history"].append({"at": now, "field": k, "label": FIELD_LABELS[k], "before": old, "after": new})
                note["fields"][k] = new
                changed.append(k)
        if name and not note.get("name"):
            note["name"] = name
        if not changed:
            return note, [], None
        note["version"] = int(note["version"]) + 1
        note["updated_at"] = now
        note["created_at"] = note.get("created_at") or now
        _write(note)
        export = export_note(note)
        return note, changed, export


def add_memo(market, code, text, source="대시보드", name=""):
    market, code = _key(market, code)
    text = str(text or "").strip()
    if not text:
        raise ValueError("메모 내용이 비어 있음")
    if source not in MEMO_SOURCES:
        source = MEMO_SOURCES[0]
    with _lock((market, code)):
        note = _read(market, code) or _empty(market, code, name)
        if name and not note.get("name"):
            note["name"] = name
        memo = {"id": uuid.uuid4().hex[:12], "at": _now(), "text": text, "source": source}
        note["memos"].append(memo)
        note["updated_at"] = memo["at"]
        note["created_at"] = note.get("created_at") or memo["at"]
        _write(note)
        return note, memo, export_note(note)


def delete_memo(market, code, memo_id):
    market, code = _key(market, code)
    with _lock((market, code)):
        note = _read(market, code)
        if not note:
            return None, False, None
        before = len(note["memos"])
        note["memos"] = [m for m in note["memos"] if m.get("id") != memo_id]
        if len(note["memos"]) == before:
            return note, False, None
        note["updated_at"] = _now()
        _write(note)
        return note, True, export_note(note)


def delete_note(market, code):
    """종목 판단 전체 삭제 (원본 파일 + Drive 파일 + 인덱스 줄). 시험 뒤 원복용."""
    market, code = _key(market, code)
    with _lock((market, code)):
        note = _read(market, code)
        try:
            os.remove(_path(market, code))
            removed = True
        except FileNotFoundError:
            removed = False
        export = {"ok": False, "error": "Drive 를 찾지 못함"}
        d = _export_dir()
        if d:
            md_removed = []
            try:
                for fn in os.listdir(d):
                    if fn.endswith(f"_{code}.md") and (not note or fn == _md_name(note)):
                        os.remove(os.path.join(d, fn))
                        md_removed.append(fn)
                idx = _write_index()
                export = {"ok": True, "dir": d, "removed": md_removed, "index": idx}
            except OSError as e:
                export = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return removed, export


def list_notes():
    """판단이나 메모가 있는 종목 목록 (마지막 수정 최신순)"""
    out = []
    if not os.path.isdir(NOTES_DIR):
        return out
    for fn in sorted(os.listdir(NOTES_DIR)):
        m = re.fullmatch(r"(KR|US)_([A-Z0-9.]+)\.json", fn)
        if not m:
            continue
        note = _read(m.group(1), m.group(2))
        if not has_content(note):
            continue
        out.append({"market": note["market"], "code": note["code"], "name": note.get("name", ""),
                    "updated_at": note.get("updated_at"), "version": note["version"],
                    "memo_count": len(note["memos"]),
                    "has_fields": any((note["fields"].get(k) or "").strip() for k in FIELD_KEYS)})
    out.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return out


# ===== Drive 내보내기 =====
def _export_dir():
    return telegram_report.drive_path(*EXPORT_SUBDIR)


def _safe(name):
    return re.sub(r'[/\\:*?"<>|]', "_", (name or "").strip()) or "종목"


def _md_name(note):
    return f"{_safe(note.get('name') or note['code'])}_{note['code']}.md"


def _d(ts):
    return (ts or "")[:10]


def _dt(ts):
    return (ts or "").replace("T", " ")[:16]


def render_markdown(note):
    lines = [f"# {note.get('name') or note['code']} ({note['code']}, {'한국' if note['market'] == 'KR' else '미국'})", "",
             f"- 마지막 수정일: {_d(note.get('updated_at')) or '-'}", f"- 판 번호: {note['version']}", "",
             "## 현재 판단", ""]
    for k in FIELD_KEYS:
        lines += [f"### {FIELD_LABELS[k]}", "", (note["fields"].get(k) or "").strip() or "(비어 있음)", ""]
    lines += ["## 메모", ""]
    memos = sorted(note["memos"], key=lambda m: m.get("at", ""), reverse=True)
    lines += [f"- {_dt(m['at'])} · {m['text']} ({m.get('source') or '대시보드'})" for m in memos] or ["(없음)"]
    lines += ["", "## 변경 이력", ""]
    hist = sorted(note["history"], key=lambda h: h.get("at", ""), reverse=True)
    if not hist:
        lines.append("(없음)")
    for h in hist:
        before = (h.get("before") or "").strip() or "(비어 있음)"
        lines += [f"- {_dt(h['at'])} · {h.get('label') or FIELD_LABELS.get(h['field'], h['field'])}",
                  "  - 이전: " + before.replace("\n", "\n    ")]
    return "\n".join(lines) + "\n"


def render_index(items=None):
    items = list_notes() if items is None else items
    lines = ["# 종목판단 인덱스", "", f"- 갱신: {_dt(_now())}", f"- 종목 수: {len(items)}", "",
             "| 종목 | 코드 | 시장 | 마지막 수정일 | 메모 수 | 파일 |", "|---|---|---|---|---|---|"]
    for it in items:
        fn = f"{_safe(it.get('name') or it['code'])}_{it['code']}.md"
        lines.append(f"| {it.get('name') or '-'} | {it['code']} | {'한국' if it['market'] == 'KR' else '미국'} | "
                     f"{_d(it.get('updated_at'))} | {it['memo_count']} | [{fn}]({fn}) |")
    return "\n".join(lines) + "\n"


def _atomic_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return len(text.encode("utf-8"))


def _write_index():
    d = _export_dir()
    with _index_lock:
        return _atomic_text(os.path.join(d, INDEX_NAME), render_index())


def export_note(note):
    """반환: {"ok", "path", "bytes", "index_path"} 또는 {"ok": False, "error"}"""
    d = _export_dir()
    if not d:
        return {"ok": False, "error": "Drive 를 찾지 못함 (원본은 저장됨)"}
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, _md_name(note))
        if has_content(note):
            n = _atomic_text(path, render_markdown(note))
        else:
            n = 0
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        _write_index()
        return {"ok": True, "path": path, "bytes": n, "index_path": os.path.join(d, INDEX_NAME)}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e} (원본은 저장됨)"}


def export_status(note):
    """Gate 검증용: Drive 파일 존재 여부와 내용"""
    d = _export_dir()
    if not d:
        return {"ok": False, "error": "Drive 를 찾지 못함"}
    path = os.path.join(d, _md_name(note))
    out = {"ok": True, "dir": d, "path": path, "exists": os.path.exists(path)}
    try:
        if out["exists"]:
            with open(path, "r", encoding="utf-8") as f:
                out["markdown"] = f.read()
        ip = os.path.join(d, INDEX_NAME)
        out["index_path"] = ip
        if os.path.exists(ip):
            with open(ip, "r", encoding="utf-8") as f:
                out["index_markdown"] = f.read()
    except OSError as e:
        out["read_error"] = f"{type(e).__name__}: {e}"
    return out
