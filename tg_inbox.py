import json
import os
import re
import time
import base64
import shutil
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
    # 자동 수록 채널: 여기 등록된 채널의 신규 글은 수집 즉시 saved=true 처리(기존 저장 파이프라인 사용)
    "auto_save_channels": [],
    # 숨김 채널: 대시보드 화면(인박스·자료 목록·건수)에서만 제외. 수집·저장·분류·전사·드라이브·감시는 유지
    "hidden_channels": [],
    "retention_days": 6,
    "poll_interval_minutes": 10,
    "max_fetch_per_channel": 200,
    # 저장(별표) 자료 드라이브 내보내기 폴더. 비우면 드라이브 마운트에서 자동 탐색
    "drive_export_dir": "",
    # 이미지 정밀 전사용 비전 모델 (분류용 flash-lite와 별개). 상위 정확도 필요 시 교체 가능.
    "vision_transcribe_model": "gemini-2.5-flash",
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


def _entity_ref(identifier):
    """get_entity에 넘길 값. 숫자 ID('-100...' 또는 순수 숫자)는 int로, 그 외는 문자열 그대로."""
    s = str(identifier).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
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
    cfg["auto_save_channels"] = [_normalize_channel(c) for c in cfg.get("auto_save_channels", [])]
    cfg["hidden_channels"] = [_normalize_channel(c) for c in cfg.get("hidden_channels", [])]
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
    entity = await client.get_entity(_entity_ref(username))
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
    auto_save_set = set(cfg.get("auto_save_channels", []))
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
        auto_saved_ids = []          # 이번 주기에 자동 저장된 항목(락 해제 후 전사 배치)
        now_iso = now_utc.isoformat()
        now_kst_iso = now_utc.astimezone(KST).isoformat()

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
                # 자동 수록 채널이면 수집 즉시 저장 처리(기존 저장 파이프라인 사용)
                if username in auto_save_set:
                    item["saved"] = True
                    item["saved_ts"] = now_kst_iso
                    item["auto_saved"] = True
                    auto_saved_ids.append(item["id"])
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

    # 자동 저장된 항목: 저장 변화 반영(dirty) + 이미지 전사를 백그라운드 배치로 처리(완료 후 export 1회)
    if auto_saved_ids:
        _export_state["dirty"] = True
        print(f"[tg_inbox] auto-save: {len(auto_saved_ids)}건 (채널 {sorted(auto_save_set)})", flush=True)
        threading.Thread(target=_batch_transcribe_then_export,
                         args=(list(auto_saved_ids),), daemon=True).start()

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
    is_numeric = bool(re.fullmatch(r"-?\d+", username))

    async def _resolve():
        if is_numeric:
            # 숫자 ID: username 해석(ResolveUsernameRequest) 대신 get_entity(int)로 재해석
            entity = await client.get_entity(_entity_ref(username))
        else:
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
        hint = ""
        if is_numeric:
            hint = " (그룹이 슈퍼그룹으로 전환되어 ID가 바뀌었을 수 있음 — 새 -100 ID로 재등록 필요)"
        print(f"[tg_inbox] refresh_channel({username}) 실패: {type(e).__name__}: {e}{hint}", flush=True)
        return {"channel": username, "error": f"{type(e).__name__}: {e}{hint}"}
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


# ===== 저장(별표) 항목 이미지 정밀 전사 (분류용과 별개, 상위 비전 모델) =====

_TRANSCRIBE_PROMPT = (
    "이 이미지는 투자 정보 텔레그램 게시물의 첨부다. 아래 형식으로 정밀 전사하라.\n"
    "1) 유형: 차트/표/기사캡처/문서/스크린샷/일반사진 중 하나\n"
    "2) 제목·출처: 이미지 안에 보이는 제목, 출처, 날짜를 원문 그대로\n"
    "3) 내용 전사:\n"
    "   - 표이면 마크다운 표로 모든 행·열을 전사 (수치·단위 포함)\n"
    "   - 차트이면 축 이름·단위·계열명과, 읽을 수 있는 모든 데이터 포인트의 수치를 나열하고 추세를 서술\n"
    "   - 텍스트 위주면 본문을 그대로 전사\n"
    "전사 제외 대상 (출력하지 마라):\n"
    "- 메신저 앱의 UI 요소: 채널명 헤더, 구독자 수, 고정 메시지 미리보기, 조회수, 시각, 버튼\n"
    "- 면책조항(Disclaimer)·저작권 고지·무단전재 금지 등 상용구 문단: 내용을 전사하지 말고 '[면책조항 생략]' 한 줄로만 표기\n"
    "- 워터마크, 로고, 광고 배너\n"
    "단, 게시물 본문·표·차트의 정보는 위 규칙과 무관하게 전부 전사한다. "
    "출처 표기(작성 기관·애널리스트명·날짜)는 상용구가 아니므로 유지한다.\n"
    "4) 핵심: 투자 관점의 요지 1~2줄\n"
    "절대 규칙: 판독할 수 없는 수치·글자는 추정하지 말고 [판독불가]로 표기하라. "
    "이미지가 흐리거나 잘려 있으면 그 사실을 명시하라. 이미지에 없는 정보를 지어내지 마라."
)

# ===== 전사 품질 감시: 건강 판정 · 통계 · 텔레그램 알림 =====

_STATS_PATH = "cache/tg_transcribe_stats.json"
_stats_lock = threading.Lock()

# 명백한 거부/오류 문구 (하나라도 포함되면 불합격)
_REFUSAL_MARKERS = (
    "죄송", "이미지를 볼 수 없", "이미지를 확인할 수 없", "볼 수 없습니다",
    "I cannot", "I can't", "I'm unable", "unable to", "cannot process",
)


def _check_transcription_health(desc_text):
    """전사문의 형식 붕괴·비정상을 판정. (ok: bool, reason: str) 반환."""
    t = (desc_text or "").strip()
    if len(t) < 50:
        return False, f"너무 짧음({len(t)}자<50)"
    if ("유형" not in t) or ("핵심" not in t):
        miss = []
        if "유형" not in t:
            miss.append("유형")
        if "핵심" not in t:
            miss.append("핵심")
        return False, "필수 구조 결여(" + "/".join(miss) + ")"
    unreadable = t.count("[판독불가]")
    if unreadable > 10:
        return False, f"판독불가 과다({unreadable}회)"
    low = t.lower()
    for m in _REFUSAL_MARKERS:
        if m.lower() in low:
            return False, f"거부/오류 문구('{m}')"
    return True, "ok"


def _load_stats():
    try:
        with open(_STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"days": {}, "api_consecutive_fail": 0,
                "last_daily_alert": "", "last_weekly_sample": ""}


def _save_stats(stats):
    os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
    tmp = _STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _STATS_PATH)


def _record_transcribe_stat(outcome, reason=None):
    """outcome: 'success' | 'fail_check' | 'fail_api'. 당일 버킷과 연속 API실패 카운터 갱신."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    with _stats_lock:
        stats = _load_stats()
        day = stats["days"].setdefault(
            today, {"attempt": 0, "success": 0, "fail_check": 0, "fail_api": 0, "reasons": []})
        day["attempt"] += 1
        day[outcome] = day.get(outcome, 0) + 1
        if outcome == "fail_api":
            stats["api_consecutive_fail"] = int(stats.get("api_consecutive_fail", 0)) + 1
        else:
            stats["api_consecutive_fail"] = 0  # API가 응답함
        if reason and outcome != "success":
            day["reasons"].append(reason)
            day["reasons"] = day["reasons"][-50:]  # 과도한 누적 방지
        # 오래된 날짜 정리(60일 초과)
        cutoff = (datetime.now(KST) - timedelta(days=60)).strftime("%Y-%m-%d")
        for d in [k for k in stats["days"] if k < cutoff]:
            del stats["days"][d]
        _save_stats(stats)


def _tg_creds():
    """기존 알림 경로 재사용: env TELEGRAM_TOKEN_GENERAL + dart_monitor_config.json general chat_id."""
    token = os.environ.get("TELEGRAM_TOKEN_GENERAL")
    if not token:
        return None, None
    try:
        with open("dart_monitor_config.json", "r", encoding="utf-8") as f:
            chat_id = (json.load(f).get("telegram_chat_ids") or {}).get("general")
    except (OSError, ValueError):
        return None, None
    return (token, chat_id) if chat_id else (None, None)


def _tg_send_text(text):
    """일반 텍스트 발송(전사문에 <,&,| 등이 있어 parse_mode 미사용). 4096자 분할. 성공 여부 반환."""
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        print("[tg_inbox] telegram creds 없음 — 발송 생략", flush=True)
        return False
    chunks, cur = [], ""
    for line in (text or "").split("\n"):
        while len(line) > 4000:
            chunks.append(line[:4000])
            line = line[4000:]
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    ok = True
    for ch in (chunks or [""]):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": ch, "disable_web_page_preview": True},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[tg_inbox] sendMessage http {r.status_code}: {r.text[:150]}", flush=True)
                ok = False
        except Exception as e:
            print(f"[tg_inbox] sendMessage error: {type(e).__name__}: {e}", flush=True)
            ok = False
        time.sleep(0.3)
    return ok


def _tg_send_photo(image_path, caption):
    """원본 이미지 첨부 발송(sendPhoto, multipart). caption은 1024자로 절단. 성공 여부 반환."""
    token, chat_id = _tg_creds()
    if not token or not chat_id:
        return False
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": (caption or "")[:1024]},
                files={"photo": f},
                timeout=30,
            )
        if r.status_code != 200:
            print(f"[tg_inbox] sendPhoto http {r.status_code}: {r.text[:150]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[tg_inbox] sendPhoto error: {type(e).__name__}: {e}", flush=True)
        return False


def _gemini_vision(prompt, image_path, model, timeout=120):
    """설정된 상위 비전 모델로 이미지 1장을 전사. 429는 RateLimitError, 그 외 실패는 ""."""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": prompt},
                ]}],
                # 정확도 우선: 온도 0(결정적). gemini-2.5-flash는 사고(thinking) 토큰이
                # maxOutputTokens 예산을 잠식해 큰 표가 잘리므로(finishReason=MAX_TOKENS),
                # 출력 예산을 크게 늘리고 사고 예산은 상한을 둔다.
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 32768,
                    "thinkingConfig": {"thinkingBudget": 2048},
                },
            },
            timeout=timeout,
        )
    except Exception:
        return ""
    if res.status_code == 429:
        raise RateLimitError("429 rate limit")
    if res.status_code != 200:
        print(f"[tg_inbox] vision {model} http {res.status_code}: {res.text[:150]}", flush=True)
        return ""
    try:
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return ""


def _transcribe_one(image_path, model):
    """이미지 1장 전사. 실패 시 재시도 1회(총 2회). 최종 실패면 "" 반환."""
    for attempt in range(2):
        try:
            txt = _gemini_vision(_TRANSCRIBE_PROMPT, image_path, model)
        except RateLimitError:
            time.sleep(20)
            continue
        if txt and txt.strip():
            return txt.strip()
    return ""


def transcribe_item_images(item_id):
    """해당 항목의 각 이미지를 정밀 전사해 item['image_desc'](이미지당 1개)에 저장.

    - 이미 desc가 있는 이미지는 건너뛴다(재호출 안 함).
    - 네트워크 호출은 _data_lock 밖에서 수행(락은 스냅샷/저장 시에만 잡음).
    - 전사 실패는 로그만 남기고 해당 이미지는 desc 없이 둔다(저장·내보내기에 영향 없음).
    """
    item_id = str(item_id or "")
    if not item_id:
        return {"error": "항목 id가 필요합니다"}
    with _data_lock:
        data = _load_data()
        target = next((it for it in data["items"] if it.get("id") == item_id), None)
        if target is None:
            return {"error": "항목을 찾을 수 없습니다", "status": 404}
        images = [os.path.basename(fn) for fn in (target.get("images") or [])]
        descs = list(target.get("image_desc") or [])
        healths = list(target.get("desc_health") or [])
    if not images:
        return {"status": "skip", "reason": "no_images"}

    model = (get_config().get("vision_transcribe_model") or "gemini-2.5-flash").strip()
    # descs·healths 길이를 이미지 수에 맞춤
    descs = (descs + [None] * len(images))[:len(images)]
    healths = (healths + [None] * len(images))[:len(images)]

    changed = False
    for i, fn in enumerate(images):
        if descs[i] and str(descs[i]).strip():
            ok_prev, _ = _check_transcription_health(str(descs[i]))
            if ok_prev:
                continue  # 이미 정상 전사됨 (불합격/잘린 전사문은 재전사)
        path = os.path.join(MEDIA_DIR, fn)
        txt = _transcribe_one(path, model)
        if txt:
            ok, reason = _check_transcription_health(txt)
            descs[i] = txt
            healths[i] = "ok" if ok else reason
            changed = True
            _record_transcribe_stat("success" if ok else "fail_check", None if ok else reason)
            tag = "ok" if ok else f"불합격({reason})"
            print(f"[tg_inbox] transcribe {tag}: {item_id} img#{i+1} ({len(txt)} chars)", flush=True)
        else:
            _record_transcribe_stat("fail_api", "API 무응답/빈응답")
            print(f"[tg_inbox] transcribe FAILED(api): {item_id} img#{i+1} ({fn}) — desc 없이 둠", flush=True)

    if not changed:
        return {"status": "nochange", "images": len(images)}

    with _data_lock:
        data = _load_data()
        target = next((it for it in data["items"] if it.get("id") == item_id), None)
        if target is None:
            return {"error": "항목을 찾을 수 없습니다(저장 시점)", "status": 404}
        target["image_desc"] = descs
        target["desc_health"] = healths
        target["desc_ts"] = datetime.now(KST).isoformat()
        _save_data(data)
    done = sum(1 for d in descs if d and str(d).strip())
    return {"status": "ok", "id": item_id, "images": len(images), "transcribed": done}


def _needs_transcription(item):
    """이미지가 있고 아직 전사되지 않은 이미지가 하나라도 있으면 True."""
    images = item.get("images") or []
    if not images:
        return False
    descs = item.get("image_desc") or []
    for i in range(len(images)):
        if i >= len(descs) or not (descs[i] and str(descs[i]).strip()):
            return True
    return False


def _has_unhealthy_desc(item):
    """전사문이 있지만 건강 판정에 불합격(잘림·형식붕괴)인 이미지가 하나라도 있으면 True (복구용)."""
    for dsc in (item.get("image_desc") or []):
        if dsc and str(dsc).strip():
            ok, _ = _check_transcription_health(str(dsc))
            if not ok:
                return True
    return False


def _transcribe_then_export(item_id):
    """백그라운드 워커: 전사 후 드라이브 문서 재생성. 모든 예외를 로그로만 흡수."""
    try:
        r = transcribe_item_images(item_id)
        print(f"[tg_inbox] transcribe result {item_id}: {r}", flush=True)
    except Exception as e:
        print(f"[tg_inbox] transcribe worker error {item_id}: {type(e).__name__}: {e}", flush=True)
    try:
        export_saved_to_drive()
    except Exception as e:
        print(f"[tg_inbox] post-transcribe export error: {type(e).__name__}: {e}", flush=True)


def _batch_transcribe_then_export(item_ids):
    """여러 항목의 이미지 전사를 순차 실행 후 드라이브 문서 1회 재생성. 예외는 로그로만 흡수."""
    done = 0
    for iid in item_ids:
        try:
            r = transcribe_item_images(iid)
            if isinstance(r, dict) and r.get("status") == "ok":
                done += 1
        except Exception as e:
            print(f"[tg_inbox] batch transcribe error {iid}: {type(e).__name__}: {e}", flush=True)
    try:
        export_saved_to_drive()
    except Exception as e:
        print(f"[tg_inbox] batch export error: {type(e).__name__}: {e}", flush=True)
    if item_ids:
        print(f"[tg_inbox] batch transcribe done: {done}/{len(item_ids)} 갱신", flush=True)


def set_auto_save(channel, enabled):
    """채널의 자동 수록 on/off. 켤 때 인박스에 남은 그 채널 기존 항목(보존기간 내)도 일괄 자동 저장."""
    ident = _normalize_channel(channel)
    if not ident:
        return {"error": "채널을 입력하세요"}
    enabled = bool(enabled)

    cfg = _read_raw_config()
    lst = [_normalize_channel(c) for c in (cfg.get("auto_save_channels") or [])]
    if enabled and ident not in lst:
        lst.append(ident)
    elif not enabled and ident in lst:
        lst = [c for c in lst if c != ident]
    cfg["auto_save_channels"] = lst
    _save_config(cfg)

    backfilled_ids = []
    if enabled:
        # 인박스에 남아 있는 그 채널 기존 항목(보존기간 내, 아직 미저장)을 일괄 자동 저장
        retention_days = int(get_config().get("retention_days", 6))
        cutoff_iso = (datetime.now(KST) - timedelta(days=retention_days)).isoformat()
        now_kst_iso = datetime.now(KST).isoformat()
        with _data_lock:
            data = _load_data()
            for it in data.get("items", []):
                if _normalize_channel(it.get("channel") or "") != ident:
                    continue
                if it.get("saved"):
                    continue
                if (it.get("date") or "") < cutoff_iso:
                    continue
                it["saved"] = True
                it["saved_ts"] = now_kst_iso
                it["auto_saved"] = True
                backfilled_ids.append(it.get("id"))
            if backfilled_ids:
                _save_data(data)
        if backfilled_ids:
            _export_state["dirty"] = True
            threading.Thread(target=_batch_transcribe_then_export,
                             args=(list(backfilled_ids),), daemon=True).start()
        else:
            # 저장 대상은 없어도 설정 변화 자체를 문서에 즉시 반영할 필요는 없음(신규 수집 시 반영)
            pass

    return {"status": "ok", "channel": ident, "auto_save": enabled,
            "backfilled": len(backfilled_ids)}


def set_hidden(channel, enabled):
    """채널 숨김 on/off. 대시보드 화면에서만 제외되며 수집·저장·전사·드라이브·감시는 유지."""
    ident = _normalize_channel(channel)
    if not ident:
        return {"error": "채널을 입력하세요"}
    enabled = bool(enabled)
    cfg = _read_raw_config()
    lst = [_normalize_channel(c) for c in (cfg.get("hidden_channels") or [])]
    if enabled and ident not in lst:
        lst.append(ident)
    elif not enabled and ident in lst:
        lst = [c for c in lst if c != ident]
    cfg["hidden_channels"] = lst
    _save_config(cfg)
    return {"status": "ok", "channel": ident, "hidden": enabled}


def backfill_transcriptions():
    """기존 saved 항목 중 이미지가 있고 전사 안 된 것 전부에 전사 실행(수동 호출용).

    완료 후 export를 1회 실행. 개별 항목 실패는 건너뛴다.
    """
    with _data_lock:
        data = _load_data()
        targets = [it.get("id") for it in data.get("items", [])
                   if it.get("saved") and (_needs_transcription(it) or _has_unhealthy_desc(it))]
    print(f"[tg_inbox] backfill start: {len(targets)}건", flush=True)
    done = 0
    for iid in targets:
        try:
            r = transcribe_item_images(iid)
            if isinstance(r, dict) and r.get("status") == "ok":
                done += 1
            print(f"[tg_inbox] backfill {iid}: {r}", flush=True)
        except Exception as e:
            print(f"[tg_inbox] backfill error {iid}: {type(e).__name__}: {e}", flush=True)
    try:
        export_saved_to_drive()
    except Exception as e:
        print(f"[tg_inbox] backfill export error: {type(e).__name__}: {e}", flush=True)
    print(f"[tg_inbox] backfill done: {done}/{len(targets)} 전사 갱신", flush=True)
    return {"status": "done", "targets": len(targets), "transcribed": done}


# ===== (A) 하드 실패 일일 감시 알림 =====

def _maybe_daily_transcribe_alert():
    """매일 21시대 1회: 당일 시도>=1 & (불합격+API실패)/시도>=30% 또는 연속 API실패>=3이면 알림.

    정상인 날은 발송하지 않는다. 데몬 루프에서 매 주기 호출(시각·중복 판정은 내부에서).
    """
    now = datetime.now(KST)
    if now.hour != 21:
        return
    today = now.strftime("%Y-%m-%d")
    with _stats_lock:
        stats = _load_stats()
        if stats.get("last_daily_alert") == today:
            return  # 오늘 이미 발송
        day = (stats.get("days") or {}).get(today)
        api_run = int(stats.get("api_consecutive_fail", 0))
    if not day or day.get("attempt", 0) < 1:
        return
    attempt = day["attempt"]
    fails = day.get("fail_check", 0) + day.get("fail_api", 0)
    ratio = fails / attempt if attempt else 0
    if ratio < 0.30 and api_run < 3:
        # 정상 → 발송 안 함(단, 오늘 판정만 마크해 재평가 반복 방지는 하지 않음: 조건 변할 수 있어 유지)
        return
    # 사유 요약(상위 5종 빈도)
    reasons = day.get("reasons", [])
    freq = {}
    for r in reasons:
        freq[r] = freq.get(r, 0) + 1
    top = sorted(freq.items(), key=lambda x: -x[1])[:5]
    reason_line = ", ".join(f"{k}×{v}" for k, v in top) if top else "사유 기록 없음"
    msg = (
        f"[전사 감시] {today} 이미지 전사 {attempt}건 중 실패 {fails}건 "
        f"(불합격 {day.get('fail_check',0)} · API실패 {day.get('fail_api',0)}, 실패율 {ratio*100:.0f}%)"
        + (f" · 연속 API실패 {api_run}건" if api_run >= 3 else "")
        + f"\n사유: {reason_line}"
    )
    if _tg_send_text(msg):
        with _stats_lock:
            stats = _load_stats()
            stats["last_daily_alert"] = today
            _save_stats(stats)
        print(f"[tg_inbox] 전사 감시 알림 발송: {today} 실패율 {ratio*100:.0f}%", flush=True)


# ===== (B) 주간 표본 검사 리포트 =====

def send_weekly_transcribe_sample():
    """최근 7일 전사된 저장 항목의 이미지 중 무작위 3장을 [원본+전사문]으로 텔레그램 발송."""
    import random
    now = datetime.now(KST)
    cutoff = now - timedelta(days=7)
    with _data_lock:
        data = _load_data()
        pool = []  # (item_id, channel_title, date, img_basename, desc)
        for it in data.get("items", []):
            if not it.get("saved"):
                continue
            descs = it.get("image_desc") or []
            imgs = it.get("images") or []
            if not any(d and str(d).strip() for d in descs):
                continue
            # 전사 시각 기준 최근 7일(desc_ts 없으면 saved_ts/date로 대체)
            ref = _parse_dt(it.get("desc_ts") or it.get("saved_ts") or it.get("date"))
            if ref is None or ref < cutoff:
                continue
            for i, base in enumerate(imgs):
                d = descs[i] if i < len(descs) else None
                if d and str(d).strip():
                    pool.append((it.get("id"), it.get("channel_title") or it.get("channel") or "",
                                 it.get("date"), os.path.basename(base), str(d)))

    if not pool:
        _tg_send_text("[전사 표본] 이번 주 전사 없음")
        with _stats_lock:
            stats = _load_stats()
            stats["last_weekly_sample"] = now.strftime("%Y-%m-%d")
            _save_stats(stats)
        print("[tg_inbox] 주간 표본: 이번 주 전사 없음", flush=True)
        return {"status": "empty", "samples": 0}

    picks = random.sample(pool, min(3, len(pool)))
    n = len(picks)
    sent = 0
    for idx, (iid, ch, dt, base, desc) in enumerate(picks, 1):
        caption = f"[전사 표본 {idx}/{n}] {ch} · {_fmt_dt(dt)}"
        path = os.path.join(MEDIA_DIR, base)
        photo_ok = _tg_send_photo(path, caption) if os.path.exists(path) else False
        if not photo_ok:
            # 원본이 없거나 첨부 실패 시 캡션만이라도 텍스트로
            _tg_send_text(caption + ("  (원본 이미지 없음)" if not os.path.exists(path) else "  (이미지 첨부 실패)"))
        _tg_send_text(f"[전사문 {idx}/{n}] {ch}\n\n{desc}")
        sent += 1
    _tg_send_text("전사문이 이미지와 다르면 알려주세요.")
    with _stats_lock:
        stats = _load_stats()
        stats["last_weekly_sample"] = now.strftime("%Y-%m-%d")
        _save_stats(stats)
    print(f"[tg_inbox] 주간 표본 발송: {sent}건 (pool {len(pool)})", flush=True)
    return {"status": "ok", "samples": sent, "pool": len(pool)}


def _check_hidden_channel_health():
    """숨김 채널이 unhealthy로 전환되는 시점에 텔레그램 1회 알림(state에 기록, 회복 시 리셋).

    숨김 채널은 UI에 ⚠가 보이지 않으므로 별도 알림이 필요. 데몬 루프에서 collect 후 호출.
    """
    cfg = get_config()
    hidden = set(cfg.get("hidden_channels", []))
    if not hidden:
        return
    now = datetime.now(timezone.utc)
    alerts = []  # (title, days) — 락 밖에서 발송
    with _data_lock:
        data = _load_data()
        state = data.get("state", {})
        title_by_ch = {}
        for it in data.get("items", []):
            ch = it.get("channel"); t = it.get("channel_title")
            if ch and t and ch not in title_by_ch:
                title_by_ch[ch] = t
        changed = False
        for ch in hidden:
            s = dict(state.get(ch, {}) or {})
            lst = s.get("last_success_ts")
            ce = int(s.get("consecutive_errors", 0) or 0)
            days = None
            if lst:
                try:
                    dt = datetime.fromisoformat(lst)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    days = (now - dt).days
                    healthy = (now - dt) <= timedelta(hours=72) and ce < 3
                except Exception:
                    healthy = False
            else:
                healthy = ce < 3  # 한 번도 성공 못했으면 연속에러로만 판정
            if not healthy and not s.get("unhealthy_alerted"):
                s["unhealthy_alerted"] = True
                state[ch] = s
                changed = True
                alerts.append((title_by_ch.get(ch, ch), days))
            elif healthy and s.get("unhealthy_alerted"):
                s["unhealthy_alerted"] = False
                state[ch] = s
                changed = True
        if changed:
            data["state"] = state
            _save_data(data)
    for title, days in alerts:
        d = f"{days}일 전" if isinstance(days, int) else "기록 없음"
        _tg_send_text(f"[인박스] 숨김 채널 {title} 수집 이상 — 마지막 성공 {d}")
        print(f"[tg_inbox] 숨김 채널 건강 알림: {title} (마지막 성공 {d})", flush=True)


def _maybe_weekly_transcribe_sample():
    """일요일 08시대 1회 주간 표본 발송(중복 방지: stats.last_weekly_sample). 데몬 루프에서 호출."""
    now = datetime.now(KST)
    if now.weekday() != 6 or now.hour != 8:  # 6=일요일
        return
    today = now.strftime("%Y-%m-%d")
    with _stats_lock:
        stats = _load_stats()
        if stats.get("last_weekly_sample") == today:
            return  # 이번 주 이미 발송
    try:
        send_weekly_transcribe_sample()
    except Exception as e:
        print(f"[tg_inbox] 주간 표본 오류: {type(e).__name__}: {e}", flush=True)


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
    auto_set = set(cfg.get("auto_save_channels", []))
    hidden_set = set(cfg.get("hidden_channels", []))
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
            "auto_save": ch in auto_set,
            "hidden": ch in hidden_set,
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
    # 숫자 ID 지원(값 변형 없이 int로 해석):
    #  - 음수(-100...=채널/슈퍼그룹, -...=구형 일반그룹): 그대로 후보 1개
    #  - 양수: '-100' 접두(채널) 우선, 실패 시 '-'값(구형 그룹)으로 재시도
    #  - 그 외: username 경로
    if re.fullmatch(r"-\d+", tmp):
        candidates = [tmp]
    elif re.fullmatch(r"\d+", tmp):
        candidates = ["-100" + tmp, "-" + tmp]
    else:
        u = _normalize_channel(s_in)
        candidates = [u] if u else []
    if not candidates:
        return {"error": "채널을 찾을 수 없습니다"}

    existing = [_normalize_channel(c) for c in get_config().get("channels", [])]
    for cand in candidates:
        if cand in existing:
            return {"error": "이미 등록된 채널입니다"}

    client = telegram_report._shared_client
    loop = telegram_report._shared_loop
    if client is None or loop is None:
        return {"error": "텔레그램 클라이언트가 준비 중입니다. 잠시 후 다시 시도하세요"}

    username = None
    title = None
    last_err = None
    for cand in candidates:
        try:
            entity = asyncio.run_coroutine_threadsafe(
                client.get_entity(_entity_ref(cand)), loop).result()
            username = cand
            title = getattr(entity, "title", None) or cand
            break
        except Exception as e:
            last_err = e
            print(f"[tg_inbox] add_channel 후보 '{cand}' 검증 실패: {type(e).__name__}: {e}", flush=True)
    if username is None:
        traceback.print_exc()
        sys.stdout.flush()
        return {"error": _classify_add_error(last_err)}

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
        need_transcribe = bool(saved) and _needs_transcription(target)
    # 별표 항목에 미전사 이미지가 있으면 백그라운드로 전사 후 문서 재반영 (UI 응답을 막지 않음)
    if need_transcribe:
        threading.Thread(target=_transcribe_then_export, args=(item_id,), daemon=True).start()
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
    descs = it.get("image_desc") or []
    for n, fn in enumerate(imgs, 1):
        base = os.path.basename(fn)
        lines.append(f"[이미지 {n} — {base}]")
        d = descs[n - 1] if n - 1 < len(descs) else None
        if d and str(d).strip():
            for dl in str(d).strip().split("\n"):
                lines.append("> " + dl)
        else:
            lines.append(f"[이미지 {n} — 전사 대기 중]")
        lines.append("")
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

        # 저장 항목의 원본 이미지를 드라이브 '이미지/' 폴더로 증분 복사(동일 파일명 존재 시 skip).
        # 별표 해제 항목의 사본은 남겨둔다(문서에서만 빠짐). 복사 실패는 로그만, 내보내기 계속.
        img_dir = os.path.join(export_dir, "이미지")
        copied = 0
        try:
            os.makedirs(img_dir, exist_ok=True)
            for it in items:
                for fn in (it.get("images") or []):
                    base = os.path.basename(fn)
                    src = os.path.join(MEDIA_DIR, base)
                    dst = os.path.join(img_dir, base)
                    if os.path.exists(dst):
                        continue  # 증분: 이미 복사됨
                    if not os.path.exists(src):
                        continue  # 원본 미디어가 이미 정리됨
                    try:
                        shutil.copy2(src, dst)
                        copied += 1
                    except OSError as ce:
                        print(f"[tg_inbox] image copy failed {base}: {ce}", flush=True)
        except OSError as e:
            print(f"[tg_inbox] image dir prepare failed: {e}", flush=True)
        if copied:
            print(f"[tg_inbox] drive image copy: +{copied}장 → {img_dir}", flush=True)

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
            # 숨김 채널 건강 감시(UI에 ⚠가 안 보이므로 텔레그램으로 알림; 실패해도 수집에 영향 없음)
            try:
                _check_hidden_channel_health()
            except Exception as e:
                print(f"[tg_inbox] hidden health check error: {type(e).__name__}: {e}", flush=True)
            # 전사 품질 감시(시각·중복 판정은 각 함수 내부에서; 실패해도 수집에 영향 없음)
            try:
                _maybe_daily_transcribe_alert()
            except Exception as e:
                print(f"[tg_inbox] daily transcribe alert error: {type(e).__name__}: {e}", flush=True)
            try:
                _maybe_weekly_transcribe_sample()
            except Exception as e:
                print(f"[tg_inbox] weekly transcribe sample error: {type(e).__name__}: {e}", flush=True)
            time.sleep(max(60, interval))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
