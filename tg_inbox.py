import json
import os
import re
import time
import base64
import hashlib
import asyncio
import concurrent.futures
import threading
import sqlite3
import sys
import traceback
from datetime import datetime, timezone, timedelta

import requests

from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

import telegram_report

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

CONFIG_PATH = "tg_inbox_config.json"
DATA_PATH = "cache/tg_inbox.json"
CORRECTIONS_PATH = "tg_inbox_corrections.json"
MEDIA_DIR = "cache/tg_media"

KST = timezone(timedelta(hours=9))

DEFAULT_CHANNELS = [
    "EarlyStock1",
    "kkkontemp",
    "fundeasy_choi",
    "cahier_de_market",
    "Macrojunglemicrolens",
    "pikachu_aje",
    "vegastooza",
    "WoosanXNNN",
]

DEFAULT_TOPICS = [
    "기업", "로봇", "뷰티", "엔터", "우주", "정책", "통신", "매크로",
    "반도체", "에너지", "암호화폐", "자율주행", "제약바이오", "2차전지", "AI",
]

DEFAULT_CONFIG = {
    "channels": DEFAULT_CHANNELS,
    "topics": DEFAULT_TOPICS,
    "retention_days": 6,
    "poll_interval_minutes": 10,
    "max_fetch_per_channel": 200,
    # 저장(별표) 자료 드라이브 내보내기 폴더. 비우면 드라이브 마운트에서 자동 탐색
    "drive_export_dir": "",
}

# 드라이브 마운트 자동 탐색은 telegram_report.drive_path() 공통 헬퍼 사용 → {드라이브 루트}/Analysis/텔레인박스
_DRIVE_EXPORT_SUBDIR = os.path.join("Analysis", "텔레인박스")
_EXPORT_INTERVAL_SEC = 24 * 3600
_export_state = {"last_ts": 0.0, "dirty": False}

_data_lock = threading.Lock()


def _normalize_channel(raw):
    s = str(raw).strip()
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^t\.me/", "", s, flags=re.IGNORECASE)
    s = s.strip("/")
    s = s.lstrip("@")
    return s


def get_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        cfg = dict(DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    cfg["channels"] = [_normalize_channel(c) for c in cfg.get("channels", [])]
    return cfg


def _read_raw_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def _save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _load_corrections():
    if not os.path.exists(CORRECTIONS_PATH):
        return []
    try:
        with open(CORRECTIONS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return d if isinstance(d, list) else d.get("corrections", [])


def _save_corrections(lst):
    tmp = CORRECTIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CORRECTIONS_PATH)


def _recent_corrections(n):
    lst = _load_corrections()
    return lst[-n:] if n else lst


def _load_data():
    if not os.path.exists(DATA_PATH):
        return {"state": {}, "dedup": {}, "items": []}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"state": {}, "dedup": {}, "items": []}
    d.setdefault("state", {})
    d.setdefault("dedup", {})
    d.setdefault("items", [])
    return d


def _save_data(data):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def _normalize_text_for_hash(text):
    s = (text or "").lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"t\.me/\S+", "", s)
    s = re.sub(r"[^0-9a-z가-힣]+", "", s)
    return s


def _text_key(text):
    norm = _normalize_text_for_hash(text)[:300]
    if not norm:
        return None
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()
    return "text:" + h


def _origin_key(msg_info):
    oc = msg_info.get("origin_channel_id")
    op = msg_info.get("origin_post_id")
    if oc and op:
        return f"fwd:{oc}:{op}"
    return f"fwd:{msg_info['own_channel_id']}:{msg_info['message_id']}"


def _is_image_message(message):
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        mime = (getattr(doc, "mime_type", "") or "")
        if mime in ("image/jpeg", "image/png"):
            return True
    return False


async def _download_image(client, message, path):
    try:
        r = await client.download_media(message, file=path, thumb=-1)
        if r:
            return r
    except Exception:
        pass
    try:
        return await client.download_media(message, file=path)
    except Exception:
        return None


def _delete_item_images(item):
    for fn in (item.get("images") or []):
        try:
            os.remove(os.path.join(MEDIA_DIR, os.path.basename(fn)))
        except OSError:
            pass


def _sweep_orphan_media(kept_items):
    if not os.path.isdir(MEDIA_DIR):
        return
    referenced = set()
    for it in kept_items:
        for fn in (it.get("images") or []):
            referenced.add(os.path.basename(fn))
    for fn in os.listdir(MEDIA_DIR):
        if fn not in referenced:
            try:
                os.remove(os.path.join(MEDIA_DIR, fn))
            except OSError:
                pass


async def _fetch_channel(client, username, last_id, limit, retention_cutoff_utc,
                         min_text_chars, dedup, local_keys):
    entity = await client.get_entity(username)
    title = getattr(entity, "title", None) or username
    own_id = getattr(entity, "id", None)
    max_id = last_id

    raw = []
    async for message in client.iter_messages(entity, min_id=last_id, limit=limit):
        if message.id > max_id:
            max_id = message.id
        raw.append(message)

    gmap = {}
    order = []
    for message in sorted(raw, key=lambda m: m.id):
        gid = getattr(message, "grouped_id", None)
        key = ("g", gid) if gid is not None else ("s", message.id)
        if key not in gmap:
            gmap[key] = []
            order.append(key)
        gmap[key].append(message)

    records = []
    for key in order:
        msgs = sorted(gmap[key], key=lambda m: m.id)
        first = msgs[0]

        mdate = first.date
        if mdate is None:
            continue
        if mdate.tzinfo is None:
            mdate = mdate.replace(tzinfo=timezone.utc)
        if mdate < retention_cutoff_utc:
            continue

        text = ""
        for m in msgs:
            t = (m.message or m.text or "")
            if t and t.strip():
                text = t
                break

        img_msgs = [m for m in msgs if _is_image_message(m)]
        has_image = len(img_msgs) > 0
        has_text = bool(text and text.strip())

        if not has_text and not has_image:
            continue
        if has_text and not has_image:
            if len(_normalize_text_for_hash(text)) < min_text_chars:
                continue

        origin_channel_id = None
        origin_post_id = None
        fwd = getattr(first, "fwd_from", None)
        if fwd is not None:
            from_id = getattr(fwd, "from_id", None)
            ch_id = getattr(from_id, "channel_id", None)
            channel_post = getattr(fwd, "channel_post", None)
            if ch_id and channel_post:
                origin_channel_id = ch_id
                origin_post_id = channel_post

        msg_info = {
            "message_id": first.id,
            "own_channel_id": own_id,
            "origin_channel_id": origin_channel_id,
            "origin_post_id": origin_post_id,
        }
        ok_key = _origin_key(msg_info)
        tx_key = _text_key(text) if has_text else None

        existing_id = None
        for k in (ok_key, tx_key):
            if not k:
                continue
            if k in dedup:
                existing_id = dedup[k]["item_id"]
                break
            if k in local_keys:
                existing_id = local_keys[k]
                break
        if existing_id is not None:
            records.append({"kind": "dup", "item_id": existing_id})
            continue

        item_id = f"{username}:{first.id}"
        images = []
        if has_image:
            os.makedirs(MEDIA_DIR, exist_ok=True)
            for seq, im in enumerate(img_msgs):
                fname = f"{own_id}_{first.id}_{seq}.jpg"
                path = os.path.join(MEDIA_DIR, fname)
                got = await _download_image(client, im, path)
                if got:
                    images.append(fname)

        item = {
            "id": item_id,
            "channel": username,
            "channel_title": title,
            "message_id": first.id,
            "date": mdate.astimezone(KST).isoformat(),
            "text": text,
            "topic": "",
            "classify_tries": 0,
            "also_in": [],
            "saved": False,
            "images": images,
        }
        records.append({"kind": "new", "item": item, "ok_key": ok_key, "tx_key": tx_key})
        local_keys[ok_key] = item_id
        if tx_key:
            local_keys[tx_key] = item_id

    return {"title": title, "max_id": max_id, "records": records, "channel_id": own_id}


async def _collect_all(client, channels, state, limit, retention_cutoff_utc, min_text_chars, dedup):
    results = {}
    local_keys = {}
    for username in channels:
        last_id = int(state.get(username, {}).get("last_message_id", 0) or 0)
        try:
            results[username] = {
                "ok": True,
                "data": await _fetch_channel(client, username, last_id, limit,
                                             retention_cutoff_utc, min_text_chars, dedup, local_keys),
            }
        except Exception as e:
            results[username] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return results


def collect_once():
    client = telegram_report._shared_client
    loop = telegram_report._shared_loop
    if client is None or loop is None:
        return {"status": "client_not_ready"}

    # 재접속 가드가 클라이언트를 살리는 동안에는 fetch를 시도하지 않고 명확한 상태를 반환
    try:
        if not client.is_connected():
            return {"status": "client_disconnected"}
    except Exception:
        return {"status": "client_disconnected"}

    cfg = get_config()
    channels = cfg.get("channels", [])
    retention_days = int(cfg.get("retention_days", 6))
    limit = int(cfg.get("max_fetch_per_channel", 200))
    min_text_chars = int(cfg.get("min_text_chars", 0))

    now_utc = datetime.now(timezone.utc)
    retention_cutoff_utc = now_utc - timedelta(days=retention_days)

    snapshot = _load_data()
    try:
        fetch = asyncio.run_coroutine_threadsafe(
            _collect_all(client, channels, snapshot.get("state", {}),
                         limit, retention_cutoff_utc, min_text_chars, snapshot.get("dedup", {})),
            loop,
        ).result(timeout=180)
    except (concurrent.futures.TimeoutError, asyncio.TimeoutError):
        # 클라이언트가 응답하지 않는 경우 무한 대기를 막고 명확한 에러를 반환
        return {"status": "error", "error": "telegram fetch timeout (클라이언트 응답 없음)"}

    with _data_lock:
        data = _load_data()
        state = data["state"]
        dedup = data["dedup"]
        items = data["items"]

        items_by_id = {it["id"]: it for it in items}

        new_count = 0
        dup_skipped = 0
        errors = {}
        refresh_needed = []          # 자동 재해석 대상(락 해제 후 처리 — 데드락 방지)
        now_iso = now_utc.isoformat()

        for username in channels:
            res = fetch.get(username)
            if res is None:
                continue
            if not res.get("ok"):
                errors[username] = res.get("error", "unknown")
                # 건강 추적: 연속 에러 증가·마지막 에러 기록(기존 필드 보존)
                entry = dict(state.get(username, {}) or {})
                entry["consecutive_errors"] = int(entry.get("consecutive_errors", 0) or 0) + 1
                entry["last_error"] = res.get("error", "unknown")
                state[username] = entry
                ce = entry["consecutive_errors"]
                # ce가 3에 도달하는 시점 1회, 이후 매 12주기(3,15,27,...)마다 1회만 자동 재해석
                if ce >= 3 and (ce - 3) % 12 == 0:
                    refresh_needed.append(username)
                continue

            cdata = res["data"]
            title = cdata["title"]
            prev_last = int(state.get(username, {}).get("last_message_id", 0) or 0)
            new_last = max(prev_last, int(cdata.get("max_id", 0) or 0))

            for rec in cdata["records"]:
                if rec["kind"] == "dup":
                    dup_skipped += 1
                    target = items_by_id.get(rec["item_id"])
                    if target is not None and title not in target.get("also_in", []) \
                            and title != target.get("channel_title"):
                        target.setdefault("also_in", []).append(title)
                    continue

                item = rec["item"]
                items.append(item)
                items_by_id[item["id"]] = item
                new_count += 1

                ts = time.time()
                dedup[rec["ok_key"]] = {"item_id": item["id"], "ts": ts}
                if rec["tx_key"]:
                    dedup[rec["tx_key"]] = {"item_id": item["id"], "ts": ts}

            # 건강 추적: 성공(신규 0건이어도 성공) — 기존 필드 보존하며 갱신
            entry = dict(state.get(username, {}) or {})
            entry["last_message_id"] = new_last
            entry["channel_id"] = cdata.get("channel_id")
            entry["last_success_ts"] = now_iso
            entry["consecutive_errors"] = 0
            entry["last_error"] = None
            state[username] = entry

        cutoff_iso = (now_utc.astimezone(KST) - timedelta(days=retention_days)).isoformat()
        # 저장(saved=true) 항목은 롤링 보존 기간이 지나도 삭제하지 않고 영구 보관한다.
        kept_items = [it for it in items if (it.get("date") or "") >= cutoff_iso or it.get("saved")]
        for it in items:
            if (it.get("date") or "") < cutoff_iso and not it.get("saved"):
                _delete_item_images(it)
        data["items"] = kept_items

        dedup_cutoff_ts = time.time() - retention_days * 86400
        data["dedup"] = {
            k: v for k, v in dedup.items()
            if float(v.get("ts", 0)) >= dedup_cutoff_ts
        }

        data["state"] = state
        _save_data(data)
        _sweep_orphan_media(kept_items)

    # 자동 복구: 락 해제 후 재해석 시도(refresh_channel이 자체적으로 락을 잡으므로 데드락 방지)
    for ch in refresh_needed:
        try:
            r = refresh_channel(ch)
            print(f"[tg_inbox] auto-refresh {ch}: {r}", flush=True)
        except Exception as e:
            print(f"[tg_inbox] auto-refresh {ch} failed: {type(e).__name__}: {e}", flush=True)

    cres = classify_pending()

    # 드라이브 내보내기: 이번 주기에 saved 변화가 있었거나(dirty) 마지막 내보내기 24시간 경과 시
    if _export_state["dirty"] or time.time() - _export_state["last_ts"] >= _EXPORT_INTERVAL_SEC:
        export_saved_to_drive()

    return {
        "status": "ok",
        "new": new_count,
        "dup_skipped": dup_skipped,
        "errors": errors,
        "classified": cres.get("classified", 0),
        "failed": cres.get("failed", 0),
        "chat_removed": cres.get("chat_removed", 0),
        "remaining": cres.get("remaining", 0),
    }


def refresh_channel(channel):
    """세션 엔티티 캐시를 우회해 채널을 강제 재해석하고, id가 바뀌었으면 state를 리셋한다.
    ResolveUsernameRequest는 플러드 제한이 엄격하므로 수동 트리거·자동 복구에서만 호출."""
    client = telegram_report._shared_client
    loop = telegram_report._shared_loop
    if client is None or loop is None:
        return {"channel": channel, "error": "client_not_ready"}
    username = (channel or "").lstrip("@").strip()
    if not username:
        return {"channel": channel, "error": "empty channel"}

    async def _resolve():
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        r = await client(ResolveUsernameRequest(username))   # 캐시 우회 + 캐시 갱신 부수효과
        entity = None
        if getattr(r, "chats", None):
            entity = r.chats[0]
        elif getattr(r, "users", None):
            entity = r.users[0]
        if entity is None:
            return None, None, None
        new_id = getattr(entity, "id", None)
        title = getattr(entity, "title", None) or username
        latest_date = None
        async for m in client.iter_messages(entity, limit=1):   # 읽기 가능 검증
            latest_date = m.date.isoformat() if m.date else None
            break
        return new_id, title, latest_date

    try:
        new_id, title, latest_date = asyncio.run_coroutine_threadsafe(
            _resolve(), loop).result(timeout=60)
    except Exception as e:
        return {"channel": username, "error": f"{type(e).__name__}: {e}"}
    if new_id is None:
        return {"channel": username, "error": "resolve returned no entity"}

    with _data_lock:
        data = _load_data()
        st = data["state"]
        entry = dict(st.get(username, {}) or {})
        old_id = entry.get("channel_id")
        id_changed = (old_id is None) or (old_id != new_id)
        state_reset = False
        if id_changed:
            entry["last_message_id"] = 0          # 새 채널 번호 체계 → 처음부터 재수집
            state_reset = True
        entry["channel_id"] = new_id
        entry["consecutive_errors"] = 0
        entry["last_error"] = None
        entry["last_success_ts"] = datetime.now(timezone.utc).isoformat()
        st[username] = entry
        data["state"] = st
        _save_data(data)                          # state의 해당 채널 항목만 갱신(dedup·items 불변)

    return {"channel": username, "old_id": old_id, "new_id": new_id,
            "id_changed": id_changed, "title": title,
            "latest_message_date": latest_date, "state_reset": state_reset}


class RateLimitError(Exception):
    pass


def _gemini(prompt):
    try:
        res = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
            f"?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
    except Exception:
        return ""
    if res.status_code == 429:
        raise RateLimitError("429 rate limit")
    try:
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return ""


def _topics_and_rules(topics, topic_definitions, corrections=None):
    topic_lines = []
    for t in topics:
        defn = topic_definitions.get(t, "")
        topic_lines.append(f"- {t}: {defn}" if defn else f"- {t}")
    topic_lines.append("- 기타: 위 어디에도 해당하지 않는 투자 관련 정보")
    topic_lines.append("- 잡담: 인사말, 이모지뿐인 글, 채널 홍보·광고, 투자 정보가 없는 일상 대화")

    example_section = ""
    if corrections:
        ex_lines = []
        for c in corrections:
            ex_text = (c.get("text") or "").replace("\n", " ")[:200]
            ex_lines.append(f"- {ex_text} → {c.get('correct_topic')}")
        example_section = (
            "사용자가 확정한 분류 예시 (이 패턴을 우선 참고하라):\n"
            + "\n".join(ex_lines)
            + "\n\n"
        )

    return (
        "허용 주제:\n"
        + "\n".join(topic_lines)
        + "\n\n경계 규칙:\n"
        "- 기업의 실적·수주·공시 뉴스는 그 기업의 주력 산업 주제로 분류한다\n"
        "- 두 산업에 걸치면 본문에서 비중이 더 큰 쪽으로 분류한다\n"
        "- AI용 반도체의 생산·공급망·장비는 반도체, AI 모델·서비스·소프트웨어는 AI\n"
        "- 특정 산업에 대한 정부 정책은 해당 산업으로, 산업이 특정되지 않으면 정책으로\n"
        "- 매크로는 거시 지표·중앙은행·시장 전반에만 사용한다\n\n"
        + example_section
    )


def _build_classify_prompt(batch, topics, topic_definitions, corrections=None):
    msg_lines = []
    for idx, m in enumerate(batch, 1):
        text = (m.get("text") or "")[:800].replace("\n", " ")
        msg_lines.append(f'{idx}. id={m["id"]}\n본문: {text}')

    return (
        "너는 한국 주식 투자자를 위한 텔레그램 뉴스 분류기다.\n"
        "아래 메시지들을 각각 허용 주제 중 정확히 하나로 분류하라.\n\n"
        + _topics_and_rules(topics, topic_definitions, corrections)
        + "반드시 JSON 배열만 출력하고 다른 텍스트는 출력하지 마라.\n"
        '형식: [{"id":"...","topic":"..."}]\n\n'
        + "\n\n".join(msg_lines)
    )


def _gemini_classify(batch, topics, topic_definitions, corrections=None):
    prompt = _build_classify_prompt(batch, topics, topic_definitions, corrections)
    raw = _gemini(prompt)
    if not raw or not raw.strip():
        raise RuntimeError("empty gemini response (quota/network)")
    out = {}
    try:
        mm = re.search(r"\[[\s\S]*\]", raw)
        if mm:
            arr = json.loads(mm.group())
            for o in arr:
                if isinstance(o, dict) and o.get("id") is not None:
                    out[str(o["id"])] = o.get("topic")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return out


def _gemini_image(prompt, image_path):
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""
    try:
        res = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
            f"?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": prompt},
            ]}]},
            timeout=60,
        )
    except Exception:
        return ""
    if res.status_code == 429:
        raise RateLimitError("429 rate limit")
    try:
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return ""


def _gemini_image_classify(item, topics, topic_definitions, corrections=None):
    images = item.get("images") or []
    if not images:
        return None
    path = os.path.join(MEDIA_DIR, os.path.basename(images[0]))
    prompt = (
        "너는 한국 주식 투자자를 위한 텔레그램 뉴스 분류기다.\n"
        "이 이미지는 투자 관련 텔레그램 채널의 게시물이다. "
        "이미지 내용(차트·표·기사 캡처 등)을 보고 주제를 하나 골라라.\n\n"
        + _topics_and_rules(topics, topic_definitions, corrections)
        + "반드시 JSON 단일 객체만 출력하고 다른 텍스트는 출력하지 마라.\n"
        '형식: {"topic":"..."}'
    )
    raw = _gemini_image(prompt, path)
    if not raw or not raw.strip():
        raise RuntimeError("empty gemini response (quota/network)")
    try:
        mm = re.search(r"\{[\s\S]*\}", raw)
        if mm:
            obj = json.loads(mm.group())
            if isinstance(obj, dict):
                return obj.get("topic")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return None


def classify_pending():
    cfg = get_config()
    topics = cfg.get("topics", [])
    topic_definitions = cfg.get("topic_definitions", {})
    valid_topics = set(topics) | {"기타"}

    corrections = _recent_corrections(20)

    with _data_lock:
        data = _load_data()
        pending = [
            it for it in data["items"]
            if it.get("topic", "") == "" and int(it.get("classify_tries", 0)) < 5
            and not it.get("manual")
        ]
    pending.sort(key=lambda x: x.get("date", ""), reverse=True)

    text_pending = [it for it in pending if (it.get("text") or "").strip()]
    image_pending = [it for it in pending if not (it.get("text") or "").strip() and (it.get("images"))]

    batches = [text_pending[i:i + 25] for i in range(0, len(text_pending), 25)][:20]

    classified = 0
    failed = 0
    chat_removed = 0
    consecutive_exc = 0
    stopped_reason = None
    stop_all = False

    for bi, batch in enumerate(batches):
        if bi > 0:
            time.sleep(2)

        results = None
        non429_exc = False
        rate_limited = False
        attempts = 0
        while True:
            try:
                results = _gemini_classify(batch, topics, topic_definitions, corrections)
                break
            except RateLimitError:
                attempts += 1
                if attempts > 2:
                    rate_limited = True
                    break
                print("[tg_inbox] classify 429, waiting 40s before retry...", flush=True)
                time.sleep(40)
            except Exception as e:
                non429_exc = True
                print(f"[tg_inbox] classify batch error: {type(e).__name__}: {e}", flush=True)
                break

        if rate_limited:
            stopped_reason = "rate_limit"
            stop_all = True
            print("[tg_inbox] classify stopped: rate_limit (batch deferred to next cycle)", flush=True)
            break

        with _data_lock:
            data = _load_data()
            items_list = data["items"]
            by_id = {it["id"]: it for it in items_list}
            remove_ids = set()
            for m in batch:
                it = by_id.get(m["id"])
                if it is None:
                    continue
                if it.get("topic", "") != "":
                    continue
                topic = (results or {}).get(m["id"])
                if topic == "잡담" and not it.get("saved"):
                    remove_ids.add(it["id"])
                    chat_removed += 1
                    continue
                if topic is not None and topic in valid_topics:
                    it["topic"] = topic
                    classified += 1
                else:
                    failed += 1
                it["classify_tries"] = int(it.get("classify_tries", 0)) + 1
                if it["classify_tries"] >= 5 and it.get("topic", "") == "":
                    it["topic"] = "기타"
            if remove_ids:
                for it in items_list:
                    if it["id"] in remove_ids:
                        _delete_item_images(it)
                data["items"] = [it for it in items_list if it["id"] not in remove_ids]
            _save_data(data)

        if non429_exc:
            consecutive_exc += 1
            if consecutive_exc >= 3:
                print("[tg_inbox] classify aborted: 3 consecutive errors", flush=True)
                stop_all = True
                break
        else:
            consecutive_exc = 0

    img_done = 0
    img_consecutive_exc = 0
    for it0 in image_pending:
        if stop_all or img_done >= 30:
            break
        time.sleep(2)
        img_done += 1

        topic = None
        non429_exc = False
        rate_limited = False
        attempts = 0
        while True:
            try:
                topic = _gemini_image_classify(it0, topics, topic_definitions, corrections)
                break
            except RateLimitError:
                attempts += 1
                if attempts > 2:
                    rate_limited = True
                    break
                print("[tg_inbox] image classify 429, waiting 40s before retry...", flush=True)
                time.sleep(40)
            except Exception as e:
                non429_exc = True
                print(f"[tg_inbox] image classify error: {type(e).__name__}: {e}", flush=True)
                break

        if rate_limited:
            stopped_reason = "rate_limit"
            print("[tg_inbox] image classify stopped: rate_limit (deferred to next cycle)", flush=True)
            break

        with _data_lock:
            data = _load_data()
            items_list = data["items"]
            it = next((x for x in items_list if x["id"] == it0["id"]), None)
            if it is not None and it.get("topic", "") == "":
                if topic == "잡담" and not it.get("saved"):
                    _delete_item_images(it)
                    data["items"] = [x for x in items_list if x["id"] != it["id"]]
                    chat_removed += 1
                else:
                    if topic is not None and topic in valid_topics:
                        it["topic"] = topic
                        classified += 1
                    else:
                        failed += 1
                    it["classify_tries"] = int(it.get("classify_tries", 0)) + 1
                    if it["classify_tries"] >= 5 and it.get("topic", "") == "":
                        it["topic"] = "기타"
                _save_data(data)

        if non429_exc:
            img_consecutive_exc += 1
            if img_consecutive_exc >= 3:
                print("[tg_inbox] image classify aborted: 3 consecutive errors", flush=True)
                break
        else:
            img_consecutive_exc = 0

    with _data_lock:
        data = _load_data()
        remaining = sum(
            1 for it in data["items"]
            if it.get("topic", "") == "" and int(it.get("classify_tries", 0)) < 5
        )

    return {"classified": classified, "failed": failed,
            "chat_removed": chat_removed, "remaining": remaining,
            "stopped_reason": stopped_reason}


def reclassify(scope):
    with _data_lock:
        data = _load_data()
        reset = 0
        for it in data["items"]:
            if it.get("manual"):
                continue
            if scope == "all" or (it.get("topic", "") or "") == scope:
                it["topic"] = ""
                it["classify_tries"] = 0
                reset += 1
        _save_data(data)

    total_classified = 0
    total_failed = 0
    total_chat_removed = 0
    remaining = 0
    for _ in range(12):
        res = classify_pending()
        total_classified += res.get("classified", 0)
        total_failed += res.get("failed", 0)
        total_chat_removed += res.get("chat_removed", 0)
        remaining = res.get("remaining", 0)
        if remaining == 0:
            break
        if res.get("stopped_reason") == "rate_limit":
            time.sleep(60)

    return {
        "scope": scope,
        "reset": reset,
        "classified": total_classified,
        "failed": total_failed,
        "chat_removed": total_chat_removed,
        "remaining": remaining,
    }


_reclassify_lock = threading.Lock()
_reclassify_status = {
    "running": False,
    "scope": None,
    "reset": 0,
    "classified": 0,
    "chat_removed": 0,
    "remaining": 0,
}


def _reclassify_worker(scope):
    try:
        with _data_lock:
            data = _load_data()
            reset = 0
            for it in data["items"]:
                if it.get("manual"):
                    continue
                if scope == "all" or (it.get("topic", "") or "") == scope:
                    it["topic"] = ""
                    it["classify_tries"] = 0
                    reset += 1
            _save_data(data)
        with _reclassify_lock:
            _reclassify_status["reset"] = reset
            _reclassify_status["remaining"] = reset

        total_classified = 0
        total_chat = 0
        remaining = reset
        for _ in range(12):
            res = classify_pending()
            total_classified += res.get("classified", 0)
            total_chat += res.get("chat_removed", 0)
            remaining = res.get("remaining", 0)
            with _reclassify_lock:
                _reclassify_status["classified"] = total_classified
                _reclassify_status["chat_removed"] = total_chat
                _reclassify_status["remaining"] = remaining
            if remaining == 0:
                break
            if res.get("stopped_reason") == "rate_limit":
                time.sleep(60)
    except Exception as e:
        print(f"[tg_inbox] reclassify worker error: {type(e).__name__}: {e}", flush=True)
    finally:
        with _reclassify_lock:
            _reclassify_status["running"] = False


def start_reclassify(scope):
    with _reclassify_lock:
        if _reclassify_status.get("running"):
            return {"status": "already_running"}
        _reclassify_status.update({
            "running": True,
            "scope": scope,
            "reset": 0,
            "classified": 0,
            "chat_removed": 0,
            "remaining": 0,
        })
    t = threading.Thread(target=_reclassify_worker, args=(scope,), daemon=True)
    t.start()
    return {"status": "started"}


def get_reclassify_status():
    with _reclassify_lock:
        return dict(_reclassify_status)


RESERVED_TOPICS = {"기타", "잡담", "미분류"}


def list_channels():
    cfg = get_config()
    data = _load_data()
    state = data.get("state", {})
    title_by_ch = {}
    for it in data.get("items", []):
        ch = it.get("channel")
        t = it.get("channel_title")
        if ch and t and ch not in title_by_ch:
            title_by_ch[ch] = t
    now = datetime.now(timezone.utc)
    out = []
    for ch in cfg.get("channels", []):
        s = state.get(ch, {}) or {}
        lst = s.get("last_success_ts")
        ce = int(s.get("consecutive_errors", 0) or 0)
        healthy = False
        if lst:
            try:
                dt = datetime.fromisoformat(lst)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                healthy = (now - dt) <= timedelta(hours=72) and ce < 3
            except Exception:
                healthy = False
        out.append({
            "channel": ch, "title": title_by_ch.get(ch, ch),
            "healthy": healthy, "last_success_ts": lst,
            "consecutive_errors": ce, "last_error": s.get("last_error"),
        })
    return out


def add_channel(raw):
    s_in = str(raw or "").strip()
    if not s_in:
        return {"error": "채널을 입력하세요"}
    tmp = re.sub(r"^https?://", "", s_in, flags=re.IGNORECASE)
    tmp = re.sub(r"^t\.me/", "", tmp, flags=re.IGNORECASE)
    tmp = tmp.strip("/").lstrip("@")
    if tmp.startswith("+"):
        return {"error": "비공개 초대 링크는 등록할 수 없습니다"}
    username = _normalize_channel(s_in)
    if not username:
        return {"error": "채널을 찾을 수 없습니다"}

    existing = [_normalize_channel(c) for c in get_config().get("channels", [])]
    if username in existing:
        return {"error": "이미 등록된 채널입니다"}

    client = telegram_report._shared_client
    loop = telegram_report._shared_loop
    if client is None or loop is None:
        return {"error": "텔레그램 클라이언트가 준비 중입니다. 잠시 후 다시 시도하세요"}

    try:
        entity = asyncio.run_coroutine_threadsafe(client.get_entity(username), loop).result()
        title = getattr(entity, "title", None) or username
    except Exception as e:
        # 원인 구분: 미존재 / 세션 SQLite 잠금 / 기타 — 전체 트레이스백은 로그로
        print(f"[tg_inbox] add_channel({username}) 검증 실패: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        return {"error": _classify_add_error(e)}

    cfg = _read_raw_config()
    cfg.setdefault("channels", [])
    cfg["channels"].append(username)
    _save_config(cfg)
    return {"status": "added", "channel": username, "title": title}


def _classify_add_error(e):
    """add_channel 검증 예외를 사용자 메시지로 구분한다."""
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError
    if isinstance(e, (UsernameNotOccupiedError, UsernameInvalidError)):
        return "채널을 찾을 수 없습니다"
    msg = str(e)
    if isinstance(e, ValueError) and ("No user has" in msg or "Cannot find any entity" in msg
                                      or "Could not find the input entity" in msg):
        return "채널을 찾을 수 없습니다"
    if isinstance(e, sqlite3.OperationalError) or "database is locked" in msg:
        return "텔레그램 세션이 일시적으로 잠겨 있습니다. 잠시 후 다시 시도해주세요"
    return f"채널 확인 중 오류: {type(e).__name__}"


def remove_channel(raw):
    username = _normalize_channel(str(raw or ""))
    if not username:
        return {"error": "채널을 입력하세요"}
    cfg = _read_raw_config()
    chans = list(cfg.get("channels", []))
    if username not in [_normalize_channel(c) for c in chans]:
        return {"error": "등록되지 않은 채널입니다"}
    cfg["channels"] = [c for c in chans if _normalize_channel(c) != username]
    _save_config(cfg)
    with _data_lock:
        data = _load_data()
        if username in data.get("state", {}):
            del data["state"][username]
        _save_data(data)
    return {"status": "removed", "channel": username}


def reorder_topics(new_order):
    if not isinstance(new_order, list):
        return {"error": "topics 배열이 필요합니다"}
    new_order = [str(t).strip() for t in new_order]
    cfg = _read_raw_config()
    current = list(cfg.get("topics", []))
    if sorted(new_order) != sorted(current):
        return {"error": "topics 구성이 현재 설정과 일치하지 않습니다 (순서만 변경 가능)"}
    cfg["topics"] = new_order
    _save_config(cfg)
    return {"status": "reordered", "topics": new_order}


def add_topic(name, definition=""):
    name = (name or "").strip()
    if not name:
        return {"error": "주제명을 입력하세요"}
    if name in RESERVED_TOPICS:
        return {"error": "예약어는 추가할 수 없습니다"}
    cfg = _read_raw_config()
    topics = cfg.setdefault("topics", [])
    if name in topics:
        return {"error": "이미 존재하는 분류입니다"}
    topics.append(name)
    definition = (definition or "").strip()
    if definition:
        cfg.setdefault("topic_definitions", {})[name] = definition
    _save_config(cfg)
    return {"status": "added", "name": name}


def remove_topic(name):
    name = (name or "").strip()
    if not name:
        return {"error": "주제명을 입력하세요"}
    if name in RESERVED_TOPICS:
        return {"error": "예약어는 삭제할 수 없습니다"}
    cfg = _read_raw_config()
    topics = list(cfg.get("topics", []))
    if name not in topics:
        return {"error": "존재하지 않는 분류입니다"}
    cfg["topics"] = [t for t in topics if t != name]
    defs = cfg.get("topic_definitions", {})
    if name in defs:
        del defs[name]
    _save_config(cfg)
    with _data_lock:
        data = _load_data()
        reset = 0
        for it in data["items"]:
            if (it.get("topic", "") or "") == name:
                it["topic"] = ""
                it["classify_tries"] = 0
                reset += 1
        _save_data(data)
    return {"status": "removed", "name": name, "reset": reset}


def correct(item_id, topic):
    item_id = str(item_id or "")
    topic = (topic or "").strip()
    if not item_id:
        return {"error": "항목 id가 필요합니다"}
    cfg = get_config()
    valid = set(cfg.get("topics", [])) | {"기타"}
    if topic not in valid:
        return {"error": "허용되지 않은 주제입니다"}

    with _data_lock:
        data = _load_data()
        target = None
        for it in data["items"]:
            if it.get("id") == item_id:
                target = it
                break
        if target is None:
            return {"error": "항목을 찾을 수 없습니다"}
        wrong = target.get("topic", "") or ""
        text_excerpt = (target.get("text") or "")[:200]
        target["wrong_topic"] = wrong
        target["topic"] = topic
        target["manual"] = True
        _save_data(data)

    corr = _load_corrections()
    corr.append({
        "text": text_excerpt,
        "wrong_topic": wrong if wrong else "미분류",
        "correct_topic": topic,
        "ts": datetime.now(KST).isoformat(timespec="seconds"),
    })
    _save_corrections(corr)
    return {"status": "corrected", "id": item_id, "topic": topic, "manual": True}


def set_saved(item_id, saved):
    item_id = str(item_id or "")
    if not item_id:
        return {"error": "항목 id가 필요합니다"}
    saved = bool(saved)
    with _data_lock:
        data = _load_data()
        target = None
        for it in data["items"]:
            if it.get("id") == item_id:
                target = it
                break
        if target is None:
            return {"error": "항목을 찾을 수 없습니다", "status": 404}
        target["saved"] = saved
        if saved:
            target["saved_ts"] = datetime.now(KST).isoformat()
        _save_data(data)
    # 저장 변경 즉시 드라이브 반영 (실패해도 저장 결과에는 영향 없음; 실패 시 dirty 유지 → 다음 수집 주기에 재시도)
    _export_state["dirty"] = True
    export_saved_to_drive()
    return {"status": "ok", "id": item_id, "saved": saved}


# ===== 저장(별표) 자료 → 구글 드라이브 내보내기 =====

ANALYSIS_GUIDE = (
    "이 문서는 사용자가 선별 저장한 투자 정보 아카이브다. 분석에 사용할 때: "
    "(1) 각 항목의 게시일시를 확인하고 최신 자료를 우선 근거로 삼을 것. "
    "(2) 오래된 항목과 최신 항목이 상충하면 최신을 따르되, 견해나 상황이 바뀌었다는 사실 자체를 분석에 언급할 것. "
    "(3) 게시일이 오래된 정보(수개월 이상)는 현재도 유효한지 유보적으로 다룰 것. "
    "(4) 갱신시각 이후의 상황은 이 문서에 없으므로 필요 시 최신 정보를 별도 확인할 것."
)


def _guide_block():
    return "> **분석 지침**\n> " + ANALYSIS_GUIDE + "\n"


def _resolve_export_dir(cfg):
    """설정값 우선, 비어 있으면 드라이브 마운트에서 '내 드라이브'/'My Drive' 실재 경로 탐색."""
    d = (cfg.get("drive_export_dir") or "").strip()
    if d:
        return os.path.expanduser(d)
    return telegram_report.drive_path(_DRIVE_EXPORT_SUBDIR)


def _migrate_saved_ts(data):
    """기존 저장 항목에 saved_ts가 없으면 게시일(date)로 소급 기입. 변경 여부 반환."""
    changed = False
    for it in data.get("items", []):
        if it.get("saved") and not it.get("saved_ts"):
            it["saved_ts"] = it.get("date") or datetime.now(KST).isoformat()
            changed = True
    return changed


def _parse_dt(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=KST)
    except Exception:
        return None


def _fmt_dt(s):
    dt = _parse_dt(s)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M") if dt else (s or "")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return len(text.encode("utf-8"))


def _render_item(it):
    ch = it.get("channel_title") or it.get("channel") or ""
    lines = [f"### [{_fmt_dt(it.get('date'))}] {ch}", ""]
    lines.append((it.get("text") or "").strip())
    lines.append("")
    if it.get("channel") and it.get("message_id"):
        lines.append(f"원문: https://t.me/{it['channel']}/{it['message_id']}")
    imgs = it.get("images") or []
    if imgs:
        lines.append(f"[이미지 {len(imgs)}장 — 대시보드에서 확인]")
    also = it.get("also_in") or []
    if also:
        lines.append("동시 게재: " + ", ".join(also))
    lines.append("")
    return "\n".join(lines)


def _render_year_file(year, items, now_str):
    by_topic = {}
    for it in items:
        by_topic.setdefault(it.get("topic") or "미분류", []).append(it)
    out = [f"# 텔레 인박스 저장자료 {year}", "",
           f"갱신시각: {now_str} · 총 {len(items)}건 · 주제 {len(by_topic)}개", "",
           _guide_block(), "",
           "각 주제 섹션 안의 항목은 게시일 최신순이다.", ""]
    for topic in sorted(by_topic, key=lambda t: (-len(by_topic[t]), t)):
        group = sorted(by_topic[topic], key=lambda x: x.get("date") or "", reverse=True)
        out.append(f"## {topic} ({len(group)}건)")
        out.append("")
        for it in group:
            out.append(_render_item(it))
    return "\n".join(out)


def _render_index(items, year_counts, now_str, now_dt):
    def brief(it):
        t = (it.get("text") or "").replace("\n", " ").strip()
        return t[:60] + ("…" if len(t) > 60 else "")
    def rows(days):
        cutoff = now_dt - timedelta(days=days)
        rs = [it for it in items if (_parse_dt(it.get("saved_ts") or it.get("date")) or now_dt) >= cutoff]
        rs.sort(key=lambda x: x.get("saved_ts") or x.get("date") or "", reverse=True)
        return rs
    out = ["# 텔레 인박스 저장자료 인덱스", "",
           f"갱신시각: {now_str} · 총 {len(items)}건", "",
           _guide_block(), ""]
    for title, days in (("최근 7일 하이라이트", 7), ("최근 30일 저장", 30)):
        rs = rows(days)
        out.append(f"## {title} ({len(rs)}건)")
        out.append("")
        if not rs:
            out.append("_해당 기간 저장 항목 없음_")
        for it in rs:
            out.append(f"- {_fmt_dt(it.get('date'))} · {it.get('channel_title') or it.get('channel')} · "
                       f"[{it.get('topic') or '미분류'}] {brief(it)} → 저장자료_{(it.get('date') or '')[:4]}.md")
        out.append("")
    out.append("## 연도별 파일")
    out.append("")
    for y in sorted(year_counts, reverse=True):
        out.append(f"- 저장자료_{y}.md — {year_counts[y]}건")
    out.append("")
    topic_counts = {}
    for it in items:
        topic_counts[it.get("topic") or "미분류"] = topic_counts.get(it.get("topic") or "미분류", 0) + 1
    out.append("## 주제별 건수")
    out.append("")
    out.append("| 주제 | 건수 |")
    out.append("|---|---|")
    for t in sorted(topic_counts, key=lambda t: (-topic_counts[t], t)):
        out.append(f"| {t} | {topic_counts[t]} |")
    out.append("")
    return "\n".join(out)


def export_saved_to_drive():
    """saved=true 항목을 드라이브 폴더에 인덱스 1개 + 연도별 md로 내보낸다.

    경로 접근 실패 등 모든 예외는 로그만 남기고 {"status": "error"}를 반환 —
    수집·저장 기능에 영향을 주지 않는다.
    """
    try:
        cfg = get_config()
        export_dir = _resolve_export_dir(cfg)
        if not export_dir:
            raise FileNotFoundError("드라이브 마운트 경로를 찾을 수 없습니다 (drive_export_dir 설정 필요)")

        with _data_lock:
            data = _load_data()
            if _migrate_saved_ts(data):
                _save_data(data)
            items = [dict(it) for it in data.get("items", []) if it.get("saved")]

        os.makedirs(export_dir, exist_ok=True)
        now_dt = datetime.now(KST)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")

        by_year = {}
        for it in items:
            by_year.setdefault((it.get("date") or "0000")[:4], []).append(it)

        files, total = {}, 0
        for year, group in by_year.items():
            name = f"저장자료_{year}.md"
            total += _atomic_write(os.path.join(export_dir, name), _render_year_file(year, group, now_str))
            files[name] = len(group)
        year_counts = {y: len(g) for y, g in by_year.items()}
        idx_name = "저장자료_인덱스.md"
        total += _atomic_write(os.path.join(export_dir, idx_name), _render_index(items, year_counts, now_str, now_dt))
        files[idx_name] = len(items)

        _export_state["last_ts"] = time.time()
        _export_state["dirty"] = False
        print(f"[tg_inbox] drive export ok: {len(items)}건 → {export_dir} ({total} bytes)", flush=True)
        return {"status": "ok", "dir": export_dir, "files": files, "bytes_total": total}
    except Exception as e:
        print(f"[tg_inbox] drive export failed: {type(e).__name__}: {e}", flush=True)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def start_inbox_collector():
    def _loop():
        waited = 0
        while telegram_report._shared_client is None and waited < 60:
            time.sleep(2)
            waited += 2
        if telegram_report._shared_client is None:
            print("[tg_inbox] shared client not ready after 60s, collector idle-starting anyway", flush=True)

        while True:
            cfg = get_config()
            interval = int(cfg.get("poll_interval_minutes", 10)) * 60
            try:
                result = collect_once()
                print(f"[tg_inbox] collect: {result}", flush=True)
            except Exception as e:
                print(f"[tg_inbox] collect error: {type(e).__name__}: {e}", flush=True)
            time.sleep(max(60, interval))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
