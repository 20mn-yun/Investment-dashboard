"""주식시황 정리: 방송(알상무·안물라·윤지호) 시황 정리를 항목 1건 = 파일 1개로 저장 (Cowork 대화 → MCP save_market_view).

저장: market_views/<id>.json, id = <source>_<YYYY-MM-DD>_<program slug>. 같은 id 는 덮어쓴다.
"""
import json
import os
import re
import threading
from datetime import date, datetime

import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
VIEWS_DIR = os.path.join(_BD, "market_views")
SOURCES = [("alsangmu", "알상무"), ("anmulla", "안물라"), ("yoonjiho", "윤지호")]
SOURCE_KEYS = [k for k, _ in SOURCES]
HISTORY_MAX = 60
ID_RE = re.compile(r"^(alsangmu|anmulla|yoonjiho)_\d{4}-\d{2}-\d{2}_[0-9A-Za-z가-힣]{1,30}$")
_lock = threading.Lock()


def _slug(program):
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", program or "")[:30]
    return s or "main"


def _path(item_id):
    return os.path.join(VIEWS_DIR, f"{item_id}.json")


def _str(v, field, limit, required=False):
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError(f"{field} 는 문자열")
    v = v.strip()
    if required and not v:
        raise ValueError(f"{field} 필수")
    if len(v) > limit:
        raise ValueError(f"{field} 는 {limit}자 이하")
    return v


def _validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("본문은 JSON 객체")
    source = payload.get("source")
    if source not in SOURCE_KEYS:
        raise ValueError(f"source 는 {'|'.join(SOURCE_KEYS)} 중 하나")
    d = _str(payload.get("date"), "date", 10, required=True)
    try:
        date.fromisoformat(d)
    except ValueError:
        raise ValueError("date 는 YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError("date 는 YYYY-MM-DD")
    program = _str(payload.get("program"), "program", 100)
    one_line = _str(payload.get("one_line"), "one_line", 500, required=True)
    buy_plan = _str(payload.get("buy_plan"), "buy_plan", 5000)
    comments = payload.get("comments") or []
    if not isinstance(comments, list) or len(comments) > 60:
        raise ValueError("comments 는 60행 이하 목록")
    rows = []
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            raise ValueError(f"comments[{i}] 는 객체")
        rows.append({"name": _str(c.get("name"), f"comments[{i}].name", 300, required=True),
                     "level": _str(c.get("level"), f"comments[{i}].level", 300),
                     "comment": _str(c.get("comment"), f"comments[{i}].comment", 300)})
    full_md = _str(payload.get("full_md"), "full_md", 80000, required=True)
    return {"source": source, "date": d, "program": program, "one_line": one_line, "buy_plan": buy_plan, "comments": rows, "full_md": full_md}


def save(payload):
    v = _validate(payload)
    item = {"id": f"{v['source']}_{v['date']}_{_slug(v['program'])}", **v, "saved_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        os.makedirs(VIEWS_DIR, exist_ok=True)
        watchlist_store.atomic_write_json(_path(item["id"]), item)
    return item


def get(item_id):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        return None
    try:
        with open(_path(item_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete(item_id):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        return False
    with _lock:
        try:
            os.remove(_path(item_id))
            return True
        except OSError:
            return False


def _all():
    out = []
    if not os.path.isdir(VIEWS_DIR):
        return out
    for fn in os.listdir(VIEWS_DIR):
        if not fn.endswith(".json") or not ID_RE.match(fn[:-5]):
            continue
        it = get(fn[:-5])
        if it:
            out.append(it)
    return out


def overview():
    items = _all()
    out = []
    for key, label in SOURCES:
        rows = sorted([i for i in items if i.get("source") == key], key=lambda i: (i.get("date") or "", i.get("saved_at") or ""), reverse=True)
        out.append({"key": key, "label": label, "latest": rows[0] if rows else None,
                    "history": [{k: i.get(k) for k in ("id", "date", "program", "one_line", "saved_at")} for i in rows[:HISTORY_MAX]]})
    return {"sources": out}
