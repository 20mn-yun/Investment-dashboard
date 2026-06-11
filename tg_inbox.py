import json
import os
import re
import time
import base64
import hashlib
import asyncio
import threading
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
}

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

    return {"title": title, "max_id": max_id, "records": records}


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

    cfg = get_config()
    channels = cfg.get("channels", [])
    retention_days = int(cfg.get("retention_days", 6))
    limit = int(cfg.get("max_fetch_per_channel", 200))
    min_text_chars = int(cfg.get("min_text_chars", 0))

    now_utc = datetime.now(timezone.utc)
    retention_cutoff_utc = now_utc - timedelta(days=retention_days)

    snapshot = _load_data()
    fetch = asyncio.run_coroutine_threadsafe(
        _collect_all(client, channels, snapshot.get("state", {}),
                     limit, retention_cutoff_utc, min_text_chars, snapshot.get("dedup", {})),
        loop,
    ).result()

    with _data_lock:
        data = _load_data()
        state = data["state"]
        dedup = data["dedup"]
        items = data["items"]

        items_by_id = {it["id"]: it for it in items}

        new_count = 0
        dup_skipped = 0
        errors = {}

        for username in channels:
            res = fetch.get(username)
            if res is None:
                continue
            if not res.get("ok"):
                errors[username] = res.get("error", "unknown")
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

            state[username] = {"last_message_id": new_last}

        cutoff_iso = (now_utc.astimezone(KST) - timedelta(days=retention_days)).isoformat()
        kept_items = [it for it in items if (it.get("date") or "") >= cutoff_iso]
        for it in items:
            if (it.get("date") or "") < cutoff_iso:
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

    cres = classify_pending()

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
                if topic == "잡담":
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
                if topic == "잡담":
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
    title_by_ch = {}
    for it in data.get("items", []):
        ch = it.get("channel")
        t = it.get("channel_title")
        if ch and t and ch not in title_by_ch:
            title_by_ch[ch] = t
    return [{"channel": ch, "title": title_by_ch.get(ch, ch)} for ch in cfg.get("channels", [])]


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
    except Exception:
        return {"error": "채널을 찾을 수 없습니다"}

    cfg = _read_raw_config()
    cfg.setdefault("channels", [])
    cfg["channels"].append(username)
    _save_config(cfg)
    return {"status": "added", "channel": username, "title": title}


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
