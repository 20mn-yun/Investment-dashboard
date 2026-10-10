"""종목별 계정 대응표 — 규칙(rule)·AI(ai)·사용자(user)가 정한 "항목 → 계정" 저장소.

파일: cache/concept_maps/<MARKET>_<코드>.json
  {"market", "code", "keep_sig", "items": {항목키: {"concept", "source", "reason", "confidence", "decided_at", "prev"?}},
   "ai_asked": "YYYY-MM-DD", "ai_log": [...], "updated_at"}
우선순위: user > ai > rule. concept 이 null 이면 "해당 없음"(그 항목은 비워 둔다).
규칙표 서명(keep_sig)이 바뀌면 rule 출처 항목만 지우고(다시 계산) ai·user 항목은 유지한다.
"""
import json
import os
import tempfile
import threading
from datetime import datetime

_BD = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(_BD, "cache", "concept_maps")
SOURCES = ("rule", "ai", "user")

_locks = {}
_locks_guard = threading.Lock()


def _lock(market, code):
    with _locks_guard:
        return _locks.setdefault((market, code), threading.RLock())


def _now():
    return datetime.now().isoformat(timespec="seconds")


def path(market, code):
    return os.path.join(DIR, f"{market.upper()}_{str(code).upper()}.json")


def load(market, code):
    try:
        with open(path(market, code), "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("items", {})
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {"market": market.upper(), "code": str(code).upper(), "items": {}}


def save(d):
    os.makedirs(DIR, exist_ok=True)
    d["updated_at"] = _now()
    p = path(d["market"], d["code"])
    fd, tmp = tempfile.mkstemp(dir=DIR, prefix="." + os.path.basename(p) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def overrides(market, code):
    """계산에 쓸 지정: {항목키: concept 또는 None(해당 없음)} — user·ai 출처만 (rule 은 매번 다시 계산)"""
    d = load(market, code)
    return {k: v.get("concept") for k, v in d["items"].items() if v.get("source") in ("ai", "user")}


def decided(market, code):
    """이미 답이 있는(ai·user, 해당 없음 포함) 항목 키"""
    return set(overrides(market, code))


def set_user(market, code, key, concept, reason=""):
    """사용자 지정. concept=None 은 "해당 없음". 이전 ai 답은 prev 에 남겨 되돌릴 수 있게."""
    with _lock(market, code):
        d = load(market, code)
        cur = d["items"].get(key)
        entry = {"concept": concept, "source": "user", "reason": reason or "사용자 지정", "confidence": "high", "decided_at": _now()}
        if cur and cur.get("source") == "ai":
            entry["prev"] = {k: v for k, v in cur.items() if k != "prev"}
        elif cur and cur.get("prev"):
            entry["prev"] = cur["prev"]
        d["items"][key] = entry
        save(d)
        return d


def delete_user(market, code, key):
    """사용자 지정 삭제 → 이전 ai 답이 있으면 복원, 없으면 항목을 지워 규칙으로 돌아간다."""
    with _lock(market, code):
        d = load(market, code)
        cur = d["items"].get(key)
        if not cur or cur.get("source") != "user":
            return d, False
        if cur.get("prev"):
            d["items"][key] = cur["prev"]
        else:
            d["items"].pop(key, None)
        save(d)
        return d, True


def set_ai(market, code, answers, meta=None):
    """AI 답 저장 (검증을 통과한 것만 넘길 것). answers: {키: {"concept", "reason", "confidence"}}. user 항목은 덮어쓰지 않는다."""
    with _lock(market, code):
        d = load(market, code)
        for key, a in answers.items():
            cur = d["items"].get(key)
            if cur and cur.get("source") == "user":
                continue
            d["items"][key] = {"concept": a.get("concept"), "source": "ai", "reason": a.get("reason", ""),
                               "confidence": a.get("confidence", "medium"), "decided_at": _now()}
        if meta:
            log = d.setdefault("ai_log", [])
            log.append(dict(meta, at=_now()))
            del log[:-10]
        save(d)
        return d


def mark_asked(market, code, day):
    with _lock(market, code):
        d = load(market, code)
        d["ai_asked"] = day
        save(d)
        return d


def set_rules(market, code, rule_concepts, keep_sig):
    """규칙이 고른 계정을 스냅샷으로 저장(source rule). 서명이 바뀌면 rule 항목만 다시 쓴다. ai·user 는 유지."""
    with _lock(market, code):
        d = load(market, code)
        if d.get("keep_sig") != keep_sig:
            d["items"] = {k: v for k, v in d["items"].items() if v.get("source") != "rule"}
            d["keep_sig"] = keep_sig
        changed = False
        for key, concept in rule_concepts.items():
            cur = d["items"].get(key)
            if cur and cur.get("source") != "rule":
                continue
            if cur and cur.get("concept") == concept:
                continue
            d["items"][key] = {"concept": concept, "source": "rule", "reason": "규칙표", "confidence": "high", "decided_at": _now()}
            changed = True
        if changed or "updated_at" not in d:
            save(d)
        return d
