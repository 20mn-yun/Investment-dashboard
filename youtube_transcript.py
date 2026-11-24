"""유튜브 자막(스크립트) → 시각 표시가 붙은 문단 본문.

MCP 도구 get_youtube_transcript 와 /youtube-transcript 페이지용. 자막은 youtube-transcript-api, 제목·채널은 oEmbed.
저장: cache/youtube_transcript/<video_id>.json (성공한 것만). 저장본이 있으면 유튜브를 다시 부르지 않는다.
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "youtube_transcript")
TIMEOUT = 20
PARA_GAP_SEC = 60
LANGS = ["ko", "en"]
KST = timezone(timedelta(hours=9))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_lock = threading.Lock()

ERR_STATUS = {"bad_request": 400, "no_transcript": 404, "video_unavailable": 404, "blocked": 502, "fetch_failed": 502}


def video_id(url_or_id):
    """watch?v=, youtu.be/, /live/, /shorts/, /embed/ 또는 ID 11자 → ID. 아니면 None."""
    s = (url_or_id or "").strip()
    if _ID_RE.match(s):
        return s
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        u = urlparse(s)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    cand = None
    if host.endswith("youtu.be"):
        cand = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] == "watch":
            cand = (parse_qs(u.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("live", "shorts", "embed", "v"):
            cand = parts[1]
    return cand if cand and _ID_RE.match(cand) else None


def _ts(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"[{h}:{m:02d}:{s:02d}]" if h else f"[{m:02d}:{s:02d}]"


def build_text(snippets):
    """조각을 이어 문단으로. 직전 시각 표시로부터 60초 이상 지난 조각에서 새 문단, 문단 앞에 [mm:ss]/[h:mm:ss]. 조각 안 줄바꿈은 공백."""
    paras, cur, mark = [], [], None
    for sn in snippets:
        start = float(sn["start"])
        txt = sn["text"].replace("\r", " ").replace("\n", " ")
        if mark is None or start - mark >= PARA_GAP_SEC:
            if cur:
                paras.append(" ".join(cur))
            cur, mark = [_ts(start)], start
        cur.append(txt)
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


def duration_text(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _TimeoutSession(requests.Session):
    def request(self, *a, **kw):
        kw.setdefault("timeout", TIMEOUT)
        return super().request(*a, **kw)


def _oembed(vid):
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=TIMEOUT)
        if r.status_code == 200:
            j = r.json()
            return j.get("title") or "", j.get("author_name") or ""
    except Exception:
        pass
    return "", ""


def _published(vid):
    """방송일(KST 날짜). watch 페이지의 startTimestamp(라이브 시작) → 없으면 uploadDate. 실패하면 ""."""
    try:
        h = requests.get(f"https://www.youtube.com/watch?v={vid}", timeout=TIMEOUT,
                         headers={"Accept-Language": "ko,en;q=0.8", "User-Agent": "Mozilla/5.0"}).text
        m = re.search(r'"startTimestamp"\s*:\s*"([^"]+)"', h) or re.search(r'"uploadDate"\s*:\s*"([^"]+)"', h)
        if m:
            return datetime.fromisoformat(m.group(1)).astimezone(KST).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _fetch_snippets(vid):
    """(transcript 메타, [{"start","duration","text"}]) — 한국어 우선, 없으면 영어. 같은 언어면 수동 > 자동."""
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi(http_client=_TimeoutSession())
    tl = api.list(vid)
    chosen = None
    for lang in LANGS:
        for finder in (tl.find_manually_created_transcript, tl.find_generated_transcript):
            try:
                chosen = finder([lang])
                break
            except Exception:
                continue
        if chosen:
            break
    if chosen is None:
        chosen = tl.find_transcript(LANGS)   # 위에서 못 찾으면 라이브러리 규칙으로 한 번 더 (없으면 NoTranscriptFound)
    fetched = chosen.fetch()
    sn = [{"start": s.start, "duration": s.duration, "text": s.text} for s in fetched.snippets]
    return {"language": fetched.language_code, "is_generated": bool(fetched.is_generated)}, sn


def _classify(e):
    import youtube_transcript_api as y
    name = type(e).__name__
    blocked = tuple(getattr(y, n) for n in ("IpBlocked", "RequestBlocked", "PoTokenRequired") if hasattr(y, n))
    unavailable = tuple(getattr(y, n) for n in ("VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId") if hasattr(y, n))
    none = tuple(getattr(y, n) for n in ("NoTranscriptFound", "TranscriptsDisabled") if hasattr(y, n))
    if blocked and isinstance(e, blocked):
        return "blocked", f"유튜브가 요청을 막았습니다({name}). 잠시 뒤 다시 시도하거나 pip install -U youtube-transcript-api 로 라이브러리를 올려 주세요."
    if unavailable and isinstance(e, unavailable):
        return "video_unavailable", f"영상을 볼 수 없습니다({name}: 비공개·삭제·연령 제한·잘못된 ID 등)."
    if none and isinstance(e, none):
        return "no_transcript", f"자막이 없거나 아직 생성되지 않았습니다({name}). 라이브 직후라면 잠시 뒤 다시 시도해 주세요."
    if isinstance(e, requests.Timeout):
        return "fetch_failed", f"유튜브 응답 없음({TIMEOUT}초)."
    return "fetch_failed", f"자막을 받지 못했습니다({name}: {str(e)[:150]})."


def _cache_path(vid):
    return os.path.join(CACHE_DIR, f"{vid}.json")


def get(url_or_id):
    """반환 (data, cached, err). data = {video_id,title,channel,published,language,is_generated,duration_sec,text,fetched_at}.
    err = (error 코드, 한국어 메시지) 또는 None."""
    vid = video_id(url_or_id)
    if not vid:
        return None, False, ("bad_request", "유튜브 주소나 11자 영상 ID 가 아닙니다 (watch?v=, youtu.be/, /live/, /shorts/, /embed/).")
    p = _cache_path(vid)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f), True, None
        except (OSError, ValueError):
            pass
    try:
        meta, sn = _fetch_snippets(vid)
    except Exception as e:
        return None, False, _classify(e)
    if not sn:
        return None, False, ("no_transcript", "자막 조각이 비어 있습니다. 라이브 직후라면 잠시 뒤 다시 시도해 주세요.")
    title, channel = _oembed(vid)
    last = sn[-1]
    data = {"video_id": vid, "title": title, "channel": channel, "published": _published(vid),
            "language": meta["language"], "is_generated": meta["is_generated"],
            "duration_sec": int(float(last["start"]) + float(last["duration"])),
            "text": build_text(sn), "fetched_at": datetime.now(KST).isoformat(timespec="seconds")}
    with _lock:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    return data, False, None


def slice_text(text, offset, max_chars):
    """offset 부터 max_chars 안에서 자른다. 남는 게 있으면 한도 안의 마지막 줄바꿈 뒤에서(없으면 글자 수로). 반환 (조각, next_offset|None)"""
    total = len(text)
    chunk = text[offset:offset + max_chars]
    if offset + len(chunk) < total:
        i = chunk.rfind("\n")
        if i > 0:
            chunk = chunk[:i + 1]
    end = offset + len(chunk)
    return chunk, (end if end < total else None)
