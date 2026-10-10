"""컨콜(실적발표 전화회의) — 미국 녹취록 요약·번역, 한국 실적발표 자료 모음.

미국: Alpha Vantage EARNINGS_CALL_TRANSCRIPT (화자·직함·발언 세그먼트). 분기는 us_financials 저장본의 최근 8개 분기에서 만들고
      회계연도 기준 "YYYYQn" 으로 바꿔 요청한다 (예: NVDA 2026.07 분기 → 2027Q2, AAPL 2026.06 → 2026Q3, MSFT 2026.06 → 2026Q4).
      저장: cache/calls/US_<티커>/<YYYYQn>.json(원문 그대로, 영구), <YYYYQn>_summary.json(Haiku 요약), <YYYYQn>_ko.json(국문 번역)
      Alpha Vantage 무료 한도: 하루 25건, 분당 5건 — cache/calls/_av_state.json 에 카운터. 넘으면 "대기"로 두고 05:30 미리 받기에서 이어서 받는다.
      AI: us_financials._call_ai(Haiku) 재사용. 한도는 ai_budget kind 'call_summary'(20/일), 'call_translate'(5/일, 녹취록 1건 = 1건).
      Drive: Analysis/컨콜/<종목명>_<티커>/<YYYYQn>_요약.md, _국문.md, _원문.md  (telegram_report.drive_path 만 사용)
한국: 공식 녹취록이 없어 3-A 리포트 목록에서 제목이 컨콜·실적발표·Review·리뷰·NDR·기업설명회에 걸리는 것과
      텔레인박스(최근 6개월)에서 종목명+키워드가 같이 있는 글을 모아 보여준다. AI 호출 없음.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import ai_budget
import telegram_report

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "calls")
AV_STATE_PATH = os.path.join(CACHE_DIR, "_av_state.json")
DRIVE_SUBDIR = ("Analysis", "컨콜")
AV_URL = "https://www.alphavantage.co/query"
AV_DAY_LIMIT = 25
AV_MINUTE_LIMIT = 5
AV_MIN_GAP_SEC = 2.0            # 연속 호출 최소 간격(초)
N_QUARTERS = 8
NONE_RECHECK_DAYS = 7            # "녹취록 없음" 응답은 7일 뒤 다시 확인 (실적 발표 직후 늦게 올라오는 경우)
LONG_TEXT_CHARS = 100_000        # 이보다 길면 준비된 발언 전체 + 질의응답 앞 60% 만 요약에 보냄
TRANSLATE_CHUNK_CHARS = 12_000   # 번역 1회 호출당 보내는 원문 글자 수 (요구: 3만 자 이하)
KR_KEYWORDS = ("컨콜", "실적발표", "실적 발표", "Review", "리뷰", "NDR", "기업설명회", "컨퍼런스콜", "컨퍼런스 콜")
TG_MONTHS = 6

_av_lock = threading.Lock()
_jobs = {}            # (code, quarter) → 번역 작업 상태
_jobs_guard = threading.Lock()
_quarters_cache = {}  # code → (updated_at, quarters)


# ---------- 공통 ----------
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _dir(code):
    return os.path.join(CACHE_DIR, f"US_{code}")


def _p(code, quarter, suffix=""):
    return os.path.join(_dir(code), f"{quarter}{suffix}.json")


def _safe(name):
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", (name or "").strip()).strip("._")


def _api_key():
    return os.environ.get("ALPHAVANTAGE_API_KEY") or ""


# ---------- Alpha Vantage 한도 ----------
def _av_state():
    st = _load_json(AV_STATE_PATH, {})
    today = date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "day_count": 0, "minute": []}
    st.setdefault("minute", [])
    st["minute"] = [t for t in st["minute"] if time.time() - t < 60]
    return st


def av_counters():
    with _av_lock:
        st = _av_state()
        return {"date": st["date"], "day_count": st["day_count"], "day_limit": AV_DAY_LIMIT,
                "minute_count": len(st["minute"]), "minute_limit": AV_MINUTE_LIMIT}


def _av_take():
    """호출 허용 여부. 반환 (허용, 사유) — 사유: None | 'day' | 'minute'"""
    with _av_lock:
        st = _av_state()
        if st["day_count"] >= AV_DAY_LIMIT:
            return False, "day"
        if len(st["minute"]) >= AV_MINUTE_LIMIT:
            return False, "minute"
        last = max(st["minute"]) if st["minute"] else 0
        gap = AV_MIN_GAP_SEC - (time.time() - last)
        if gap > 0:                       # 연속 요청은 거절당하므로 (1 request per second 안내) 간격을 둔다
            time.sleep(gap)
        st["day_count"] += 1
        st["minute"].append(time.time())
        _save_json(AV_STATE_PATH, st)
        return True, None


# ---------- 분기 목록 ----------
def av_quarter(label, fiscal_month):
    """us_financials 분기 라벨('YYYY.MM', 분기 말) + 결산월 → Alpha Vantage 회계연도 분기 'YYYYQn'"""
    y, m = int(label[:4]), int(label[5:7])
    fm = int(fiscal_month or 12)
    fy = y if m <= fm else y + 1
    q = ((m - fm - 1) % 12 + 1) // 3
    return f"{fy}Q{q}"


def quarters(code, n=N_QUARTERS):
    """저장본의 최근 n개 분기 → [{label, quarter, end, filed}] (오래된 순). 저장본 없으면 []"""
    import us_financials
    cache = us_financials._load_cache(code)
    if not cache:
        return []
    key = cache.get("updated_at")
    hit = _quarters_cache.get(code)
    if hit and hit[0] == key:
        return hit[1]
    out = us_financials.compute(cache, 10, code, persist=False)
    fm = out.get("fiscal_month") or 12
    labels = out["periods"]["quarter"][-n:]
    avail = out["available_from"]["quarter"]
    res = []
    for lb in labels:
        f = avail.get(lb)
        res.append({"label": lb, "quarter": av_quarter(lb, fm), "end": lb,
                    "filed": f"{f[:4]}-{f[4:6]}-{f[6:8]}" if f else None})
    _quarters_cache[code] = (key, res)
    return res


# ---------- 녹취록 받기 ----------
def transcript_state(code, quarter):
    """저장 상태: ('ok', data) | ('none', marker) | ('missing', None)"""
    d = _load_json(_p(code, quarter), None)
    if not d:
        return "missing", None
    if d.get("transcript"):
        return "ok", d
    return "none", d


def fetch_transcript(code, quarter, force=False):
    """녹취록 1건 받기. 반환 {status: ok|none|waiting|error, reason?}. 저장본이 있으면 재호출 없음."""
    st, d = transcript_state(code, quarter)
    if st == "ok" and not force:
        return {"status": "ok", "cached": True}
    if st == "none" and not force:
        checked = d.get("_checked") or ""
        if checked and (date.today() - date.fromisoformat(checked[:10])).days < NONE_RECHECK_DAYS:
            return {"status": "none", "cached": True}
    key = _api_key()
    if not key:
        return {"status": "error", "reason": "ALPHAVANTAGE_API_KEY 없음"}
    ok, why = _av_take()
    if not ok:
        return {"status": "waiting", "reason": {"day": f"하루 {AV_DAY_LIMIT}건 한도", "minute": f"분당 {AV_MINUTE_LIMIT}건 한도"}[why]}
    url = AV_URL + "?" + urllib.parse.urlencode({"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": code, "quarter": quarter, "apikey": key})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "investment-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:   # 키가 든 주소는 로그에 남기지 않는다
        print(f"[earnings_calls] {code} {quarter} 녹취록 요청 실패: {type(e).__name__}", flush=True)
        return {"status": "error", "reason": type(e).__name__}
    if isinstance(data, dict) and data.get("transcript"):
        data["_fetched_at"] = _now()
        _save_json(_p(code, quarter), data)
        print(f"[earnings_calls] {code} {quarter} 녹취록 저장: 세그먼트 {len(data['transcript'])}", flush=True)
        try:
            export_drive(code, quarter)          # 원문.md 는 받자마자 Drive 로 (요약·번역은 각자 끝날 때 추가)
        except Exception as e:
            print(f"[earnings_calls] Drive 내보내기 실패: {type(e).__name__}", flush=True)
        return {"status": "ok", "segments": len(data["transcript"])}
    msg = " ".join(str(v) for k, v in (data.items() if isinstance(data, dict) else []) if k in ("Information", "Note", "Error Message"))
    if re.search(r"rate limit|per minute|per day|premium|requests", msg, re.I) and "transcript" not in data:
        print(f"[earnings_calls] {code} {quarter} Alpha Vantage 한도 응답: {msg[:120]}", flush=True)
        return {"status": "waiting", "reason": "Alpha Vantage 한도 응답"}
    # 녹취록 없음 (빈 배열 또는 오류 메시지) → 표시만 저장
    _save_json(_p(code, quarter), {"symbol": code, "quarter": quarter, "transcript": [], "_checked": _now(), "_message": msg[:300]})
    return {"status": "none", "message": msg[:200]}


# ---------- 세그먼트 처리 ----------
def _is_operator(seg):
    return "operator" in (seg.get("title") or "").lower()


def _is_analyst(seg):
    return "analyst" in (seg.get("title") or "").lower()


def split_sections(transcript):
    """(준비된 발언 세그먼트, 질의응답 세그먼트). 질의응답 시작 = 처음으로 질문 안내를 하는 오퍼레이터 발언 또는 첫 애널리스트 발언."""
    qa_start = None
    for i, s in enumerate(transcript):
        if _is_analyst(s):
            qa_start = i
            break
        if _is_operator(s) and i > 0 and re.search(r"question", s.get("content") or "", re.I):
            qa_start = i
            break
    if qa_start is None:
        return transcript, []
    return transcript[:qa_start], transcript[qa_start:]


def _fmt(segs):
    return "\n\n".join(f"[{s.get('speaker')} ({s.get('title')})]\n{s.get('content', '')}" for s in segs)


def copy_text(segs):
    """'화자(직함): 발언' 한 줄씩"""
    return "\n".join(f"{s.get('speaker')}({s.get('title')}): {s.get('content', '')}" for s in segs)


# ---------- 요약 ----------
SUMMARY_SYSTEM = """당신은 미국 기업 실적발표 컨퍼런스콜 녹취록을 한국 투자자용으로 정리하는 분석 보조자입니다. 주어진 녹취록(경영진 준비된 발언과 질의응답)만 근거로 JSON 하나를 만듭니다.

규칙
- 모든 내용은 한국어로 쓴다. 회사명·제품명·고유명사는 원문 표기를 써도 된다.
- 숫자(매출, 증가율, 마진, 가이던스 범위 등)는 원문에 있는 값을 그대로 쓰고 단위를 붙인다(예: $46.7B, +56% YoY, 73.5%). 원문에 없는 숫자를 만들지 않는다.
- 가이던스는 다음 분기와 연간을 나눠 적는다. 원문에 없으면 빈 배열. 각 줄에 항목·숫자·범위를 포함한다.
- 질의응답은 중요한 5개를 고르고, 각각 질문 한 줄(질문자 소속 포함)과 답 한 줄(답한 경영진 포함)로 쓴다.
- 톤은 긍정|중립|부정 중 하나와 한 줄 근거.
- 출력은 JSON 하나만. 설명·마크다운·코드펜스 금지. 형식:
{"실적핵심": ["", "", ""],
 "가이던스": {"다음분기": ["", ""], "연간": [""]},
 "전망": ["", "", ""],
 "리스크": ["", ""],
 "질의응답": [{"질문": "", "답": ""}],
 "톤": {"판정": "긍정|중립|부정", "근거": ""}}"""


def _parse_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("JSON 없음")
    return json.loads(text[i:j + 1])


def _summary_input(transcript):
    prepared, qa = split_sections(transcript)
    p_txt, q_txt = _fmt(prepared), _fmt(qa)
    total = len(p_txt) + len(q_txt)
    truncated = False
    if total > LONG_TEXT_CHARS:
        q_txt = q_txt[:int(len(q_txt) * 0.6)]
        truncated = True
    return (f"## 경영진 준비된 발언 ({len(prepared)}개 발언)\n\n{p_txt}\n\n"
            f"## 질의응답 ({len(qa)}개 발언{', 앞 60%만' if truncated else ''})\n\n{q_txt}"), {"prepared": len(prepared), "qa": len(qa), "truncated": truncated, "chars": total}


def _check_summary(summ):
    out = {"실적핵심": [str(x) for x in (summ.get("실적핵심") or [])][:5],
           "가이던스": {"다음분기": [str(x) for x in ((summ.get("가이던스") or {}).get("다음분기") or [])][:6],
                      "연간": [str(x) for x in ((summ.get("가이던스") or {}).get("연간") or [])][:6]},
           "전망": [str(x) for x in (summ.get("전망") or [])][:5],
           "리스크": [str(x) for x in (summ.get("리스크") or [])][:4],
           "질의응답": [{"질문": str(x.get("질문") or ""), "답": str(x.get("답") or "")} for x in (summ.get("질의응답") or []) if isinstance(x, dict)][:5],
           "톤": {"판정": str((summ.get("톤") or {}).get("판정") or ""), "근거": str((summ.get("톤") or {}).get("근거") or "")}}
    if out["톤"]["판정"] not in ("긍정", "중립", "부정"):
        out["톤"]["판정"] = "중립" if not out["톤"]["판정"] else out["톤"]["판정"]
    return out


def summarize(code, quarter, name="", limit=None, force=False):
    """요약 1건 (한도 kind='call_summary'). 반환 {status: ok|cached|no_transcript|ai_limit|ai_error, ...}"""
    import us_financials
    if not force:
        cur = _load_json(_p(code, quarter, "_summary"), None)
        if cur and cur.get("status") == "ok":
            return {"status": "cached", "summary": cur}
    st, d = transcript_state(code, quarter)
    if st != "ok":
        return {"status": "no_transcript"}
    allowed, n = ai_budget.take("call_summary", limit=limit)
    if not allowed:
        return {"status": "ai_limit", "used": n}
    user_text, meta = _summary_input(d["transcript"])
    user = f"종목: {name or code} ({code})\n분기: {quarter} (회계연도 기준)\n\n{user_text}"
    t0 = time.time()
    try:
        text, tin, tout = us_financials._call_ai(SUMMARY_SYSTEM, user, max_tokens=4000)
        summ = _check_summary(_parse_json(text))
    except Exception as e:
        print(f"[earnings_calls] {code} {quarter} 요약 실패: {type(e).__name__}: {str(e)[:120]}", flush=True)
        res = {"status": f"ai_error:{type(e).__name__}", "error": str(e)[:300], "at": _now()}
        _save_json(_p(code, quarter, "_summary"), res)
        return res
    res = {"status": "ok", "quarter": quarter, "code": code, "summary": summ, "tokens": {"input": tin, "output": tout},
           "input_meta": meta, "seconds": round(time.time() - t0, 1), "at": _now(), "model": us_financials.AI_MODEL}
    _save_json(_p(code, quarter, "_summary"), res)
    print(f"[earnings_calls] {code} {quarter} 요약 완료: 토큰 {tin}+{tout}, 오늘 {n}/{ai_budget.resolve_limit('call_summary', limit)}", flush=True)
    try:
        export_drive(code, quarter, name)
    except Exception as e:
        print(f"[earnings_calls] Drive 내보내기 실패: {type(e).__name__}", flush=True)
    return res


# ---------- 번역 ----------
TRANSLATE_SYSTEM = """당신은 미국 기업 실적발표 컨퍼런스콜 녹취록을 한국어로 옮기는 전문 번역가입니다.

규칙
- 번호가 붙은 발언 묶음이 주어진다. 각 발언을 자연스러운 한국어(경어체, "~습니다")로 번역한다. 요약·생략·추가 금지.
- 숫자·단위·회사명·제품명·사람 이름·직함은 원문 그대로 둔다(예: $46.7B, Blackwell, Colette Kress). 금융 용어는 통용 표현을 쓴다(gross margin → 매출총이익률, guidance → 가이던스, YoY → 전년 대비).
- 금액은 환산하지 않는다. "$96 billion" 은 "960억 달러" 로 바꾸지 말고 "$96 billion" 그대로 쓴다. billion 을 "억" 으로 옮기면 틀리기 쉬우므로 금지한다.
- 출력은 JSON 하나만: {"1": "번역문", "2": "번역문", ...}. 키는 입력 번호와 정확히 같아야 하고 빠지면 안 된다. 설명·마크다운·코드펜스 금지."""


def _chunks(transcript, max_chars=TRANSLATE_CHUNK_CHARS):
    out, cur, size = [], [], 0
    for i, s in enumerate(transcript):
        L = len(s.get("content") or "")
        if cur and size + L > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += L
    if cur:
        out.append(cur)
    return out


def _job_key(code, quarter):
    return (code, quarter)


def translate_job(code, quarter):
    with _jobs_guard:
        j = _jobs.get(_job_key(code, quarter))
        return dict(j) if j else None


def start_translate(code, quarter, name="", limit=None):
    """번역 시작(뒤에서). 한도 kind='call_translate' 는 녹취록 1건당 1건. 반환 job 상태 dict"""
    k = _job_key(code, quarter)
    with _jobs_guard:
        j = _jobs.get(k)
        if j and j["stage"] not in ("done", "failed"):
            return dict(j)
    cur = _load_json(_p(code, quarter, "_ko"), None)
    if cur and cur.get("status") == "ok":
        return {"stage": "done", "cached": True, "done": cur.get("calls"), "total": cur.get("calls")}
    st, d = transcript_state(code, quarter)
    if st != "ok":
        return {"stage": "failed", "error": "녹취록 없음"}
    allowed, n = ai_budget.take("call_translate", limit=limit)
    if not allowed:
        return {"stage": "failed", "error": f"번역 일일 한도 도달 (오늘 {n}/{ai_budget.resolve_limit('call_translate', limit)})", "ai_limit": True}
    job = {"stage": "queued", "done": 0, "total": len(_chunks(d["transcript"])), "calls": 0, "started": _now(), "error": None}
    with _jobs_guard:
        _jobs[k] = job
    threading.Thread(target=_run_translate, args=(code, quarter, name, d["transcript"], job), daemon=True).start()
    return dict(job)


def _run_translate(code, quarter, name, transcript, job):
    import us_financials
    t0 = time.time()
    ko = [None] * len(transcript)
    tokens = {"input": 0, "output": 0}
    missing = []
    try:
        job["stage"] = "translating"
        for ci, idxs in enumerate(_chunks(transcript), 1):
            user = "\n\n".join(f"[{i + 1}] ({transcript[i].get('speaker')}, {transcript[i].get('title')})\n{transcript[i].get('content', '')}" for i in idxs)
            text, tin, tout = us_financials._call_ai(TRANSLATE_SYSTEM, user, max_tokens=16000)
            tokens["input"] += tin or 0
            tokens["output"] += tout or 0
            job["calls"] += 1
            try:
                parsed = _parse_json(text)
            except Exception:
                parsed = {}
            for i in idxs:
                v = parsed.get(str(i + 1))
                if v:
                    ko[i] = str(v)
                else:
                    ko[i] = transcript[i].get("content", "")   # 번역 못 받은 세그먼트는 원문 유지 + 표시
                    missing.append(i)
            job["done"] = ci
        segs = [{"speaker": s.get("speaker"), "title": s.get("title"), "content": ko[i], "untranslated": i in missing}
                for i, s in enumerate(transcript)]
        res = {"status": "ok", "code": code, "quarter": quarter, "segments": segs, "calls": job["calls"], "tokens": tokens,
               "seconds": round(time.time() - t0, 1), "missing": len(missing), "at": _now(), "model": us_financials.AI_MODEL}
        _save_json(_p(code, quarter, "_ko"), res)
        job.update(stage="done", seconds=res["seconds"], tokens=tokens, missing=len(missing))
        print(f"[earnings_calls] {code} {quarter} 번역 완료: 호출 {job['calls']}, {res['seconds']}s, 토큰 {tokens['input']}+{tokens['output']}, 미번역 {len(missing)}", flush=True)
        try:
            export_drive(code, quarter, name)
        except Exception as e:
            print(f"[earnings_calls] Drive 내보내기 실패: {type(e).__name__}", flush=True)
    except Exception as e:
        job.update(stage="failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        print(f"[earnings_calls] {code} {quarter} 번역 실패: {type(e).__name__}: {str(e)[:120]}", flush=True)


# ---------- 전문 ----------
def transcript(code, quarter, lang="en"):
    """세그먼트 배열. lang=ko 는 번역본(없으면 None)"""
    st, d = transcript_state(code, quarter)
    if st != "ok":
        return None
    if lang == "ko":
        ko = _load_json(_p(code, quarter, "_ko"), None)
        if not ko or ko.get("status") != "ok":
            return None
        return {"code": code, "quarter": quarter, "lang": "ko", "segments": ko["segments"], "calls": ko.get("calls"), "at": ko.get("at")}
    return {"code": code, "quarter": quarter, "lang": "en",
            "segments": [{"speaker": s.get("speaker"), "title": s.get("title"), "content": s.get("content", "")} for s in d["transcript"]],
            "at": d.get("_fetched_at")}


# ---------- Drive ----------
def _summary_md(code, quarter, name, summ):
    s = summ["summary"]
    L = [f"# {name or code} ({code}) {quarter} 컨콜 요약", "", f"- 생성: {summ.get('at')} · 모델 {summ.get('model')} · 토큰 {summ.get('tokens')}", ""]
    L += ["## 실적 핵심"] + [f"- {x}" for x in s["실적핵심"]] + [""]
    L += ["## 가이던스", "다음 분기:"] + [f"- {x}" for x in s["가이던스"]["다음분기"]] + ["", "연간:"] + [f"- {x}" for x in s["가이던스"]["연간"]] + [""]
    L += ["## 섹터·수요 전망"] + [f"- {x}" for x in s["전망"]] + [""]
    L += ["## 리스크·우려"] + [f"- {x}" for x in s["리스크"]] + [""]
    L += ["## 질의응답 핵심"] + [f"- Q. {x['질문']}\n  A. {x['답']}" for x in s["질의응답"]] + [""]
    L += ["## 톤", f"- {s['톤']['판정']}: {s['톤']['근거']}", ""]
    return "\n".join(L)


def export_drive(code, quarter, name=""):
    folder = telegram_report.drive_path(*DRIVE_SUBDIR, code)          # 미국 종목은 티커만 (3-C 규칙, stock_reports.drive_folder_name 과 같음)
    if not folder:
        return None
    os.makedirs(folder, exist_ok=True)
    written = []
    st, d = transcript_state(code, quarter)
    if st == "ok":
        p = os.path.join(folder, f"{quarter}_원문.md")
        if not os.path.exists(p):
            body = "\n\n".join(f"**{s.get('speaker')} ({s.get('title')})**\n\n{s.get('content', '')}" for s in d["transcript"])
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"# {name or code} ({code}) {quarter} 컨콜 원문\n\n{body}\n")
            written.append(p)
    summ = _load_json(_p(code, quarter, "_summary"), None)
    if summ and summ.get("status") == "ok":
        p = os.path.join(folder, f"{quarter}_요약.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(_summary_md(code, quarter, name, summ))
        written.append(p)
    ko = _load_json(_p(code, quarter, "_ko"), None)
    if ko and ko.get("status") == "ok":
        p = os.path.join(folder, f"{quarter}_국문.md")
        body = "\n\n".join(f"**{s.get('speaker')} ({s.get('title')})**\n\n{s.get('content', '')}" for s in ko["segments"])
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# {name or code} ({code}) {quarter} 컨콜 국문\n\n{body}\n")
        written.append(p)
    return written


# ---------- 상태 (API) ----------
def status_us(code, name=""):
    qs = quarters(code)
    rows = []
    for q in reversed(qs):   # 최신 먼저
        st, d = transcript_state(code, q["quarter"])
        summ = _load_json(_p(code, q["quarter"], "_summary"), None)
        ko = _load_json(_p(code, q["quarter"], "_ko"), None)
        tj = translate_job(code, q["quarter"])
        rows.append({
            "quarter": q["quarter"], "label": q["label"], "filed": q["filed"],
            "transcript": st if st != "missing" else "missing",      # ok | none | missing
            "segments_n": len(d["transcript"]) if st == "ok" else 0,
            "chars": sum(len(s.get("content") or "") for s in d["transcript"]) if st == "ok" else 0,
            "fetched_at": d.get("_fetched_at") if d else None,
            "summary": summ["summary"] if summ and summ.get("status") == "ok" else None,
            "summary_status": summ.get("status") if summ else None,
            "summary_tokens": summ.get("tokens") if summ else None,
            "translation": bool(ko and ko.get("status") == "ok"),
            "translate_job": tj,
        })
    return {"market": "US", "code": code, "name": name, "quarters": rows, "av": av_counters(),
            "ai": {"summary_used": ai_budget.used("call_summary"), "summary_limit": ai_budget.DEFAULT_LIMITS["call_summary"],
                   "translate_used": ai_budget.used("call_translate"), "translate_limit": ai_budget.DEFAULT_LIMITS["call_translate"]}}


def ensure_recent(code, name="", n=2, summarize_too=True, summary_limit=None):
    """최근 n개 분기 녹취록 받기(+요약). 05:30 미리 받기와 화면 첫 열기에서 쓴다. AV 한도 넘으면 waiting 으로 두고 끝."""
    out = []
    for q in list(reversed(quarters(code)))[:n]:
        r = fetch_transcript(code, q["quarter"])
        row = {"quarter": q["quarter"], "transcript": r.get("status")}
        if r.get("status") == "ok" and summarize_too:
            s = summarize(code, q["quarter"], name, limit=summary_limit)
            row["summary"] = s.get("status")
        out.append(row)
        if r.get("status") == "waiting":
            break
    return out


# ---------- 한국 ----------
_KW_RE = re.compile("|".join(
    [(r"(?<!프)" if k == "리뷰" else "") + re.escape(k) for k in KR_KEYWORDS if not re.match(r"^[A-Za-z]+$", k)] +     # '프리뷰' 는 리뷰가 아님
    [rf"(?<![A-Za-z]){re.escape(k)}(?![a-z])" for k in KR_KEYWORDS if re.match(r"^[A-Za-z]+$", k)]), re.I)


def is_call_related(title):
    """제목이 컨콜·실적발표 관련인지 (Review 는 Preview 에 안 걸리게 단어 경계)"""
    return bool(_KW_RE.search(title or ""))


def _same_paragraph_hit(text, variants):
    """같은 문단(빈 줄로 나뉜 덩어리) 안에 종목명과 키워드가 함께 있는지. 있으면 그 문단, 없으면 None"""
    for para in re.split(r"\n\s*\n", text or ""):
        if any(v in para for v in variants) and is_call_related(para):
            return para
    return None


def status_kr(code, name=""):
    import stock_reports
    rp = stock_reports.status("KR", code, name, start=False)
    items, seen = [], set()
    for it in rp.get("items", []):
        if is_call_related(it.get("title")):
            seen.add((it.get("channel"), it.get("message_id")))
            items.append({"source": "리포트", "date": (it.get("date") or "")[:10], "broker": it.get("broker"), "title": it.get("title"),
                          "summary": it.get("summary") or [], "opinion": it.get("opinion"), "target": it.get("target"),
                          "message_id": it.get("message_id"), "channel": it.get("channel"), "ai_status": it.get("ai_status")})
    # 3-A 의 40건 상한 밖도 리포트 DB 에서 직접 검색 (최근 6개월, 키워드 제목만). 요약은 없으므로 제목·증권사만
    try:
        found, _stats = stock_reports.search_reports("KR", code, name, months=TG_MONTHS, limit=400)
        variants_t = [v.lower() for v in stock_reports.name_variants("KR", code, name) if not v.isdigit()]
        for f in found:
            if (f["channel"], f["message_id"]) in seen:
                continue
            title = re.sub(r"\.pdf$", "", f.get("filename") or "", flags=re.I).replace("_", " ")
            if is_call_related(title) and any(v in title.lower() for v in variants_t):     # 제목에 종목명이 있어야 (본문 언급만으로 잡힌 타사 리포트 제외)
                seen.add((f["channel"], f["message_id"]))
                items.append({"source": "리포트", "date": (f.get("date") or "")[:10], "broker": f.get("broker"), "title": title,
                              "summary": [], "message_id": f["message_id"], "channel": f["channel"], "ai_status": "not_collected"})
    except Exception as e:
        print(f"[earnings_calls] 리포트 DB 검색 실패: {type(e).__name__}", flush=True)
    # 텔레인박스 (최근 6개월): 같은 문단 안에 종목명 + 키워드
    try:
        import tg_inbox
        variants = [v for v in stock_reports.name_variants("KR", code, name) if not v.isdigit()]
        since = (datetime.now() - timedelta(days=30 * TG_MONTHS)).isoformat()
        data = tg_inbox._load_data()
        for p in data.get("items", []):
            txt = p.get("text") or ""
            if (p.get("date") or "") < since or not txt:
                continue
            para = _same_paragraph_hit(txt, variants)
            if para:
                lines = [l for l in txt.strip().splitlines() if l.strip()]
                items.append({"source": "텔레그램", "date": (p.get("date") or "")[:10], "broker": p.get("channel_title"), "title": lines[0][:80] if lines else "",
                              "summary": [l for l in para.strip().splitlines() if l.strip()][:8], "message_id": p.get("message_id"),
                              "channel": p.get("channel"), "ai_status": None})
    except Exception as e:
        print(f"[earnings_calls] 텔레인박스 조회 실패: {type(e).__name__}", flush=True)
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {"market": "KR", "code": code, "name": name, "items": items, "count": len(items),
            "keywords": list(KR_KEYWORDS), "reports_total": rp.get("count")}
