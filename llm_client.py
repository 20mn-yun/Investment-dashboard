"""LLM 공통 클라이언트: Claude Code CLI(구독 로그인)를 기본으로 쓰고, 실패하면 Anthropic API(Haiku)로 폴백한다.

사용: result = llm_client.complete("reports", prompt, system=..., json_mode=True); result.ok / result.text / result.data
설정(환경변수): LLM_BACKEND=cli|api, LLM_CLI_PATH, LLM_API_FALLBACK=1|0, LLM_MAX_CONCURRENT
로그: logs/llm_calls.jsonl (호출 1건 1줄)
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta

_BD = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_BD, "logs")
LOG_PATH = os.path.join(LOG_DIR, "llm_calls.jsonl")

TASK_TIERS = {
    "us_wrap": "opus", "moat": "opus",
    "call_summary": "sonnet", "news_rank": "sonnet", "stock_intro": "sonnet", "biz_overview": "sonnet",
    "peers": "sonnet", "dart_watch_summary": "sonnet", "dart_detail": "sonnet", "earnings_extract": "sonnet",
    "reports": "haiku", "concept_map": "haiku", "ksic": "haiku",
    "call_translate": "haiku", "stock_news": "haiku",
}
TASK_BACKEND = {"call_translate": "api"}
API_MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-haiku-4-5-20251001", "opus": "claude-haiku-4-5-20251001"}
TASK_TIMEOUTS = {"call_translate": 600, "call_summary": 180}
DEFAULT_TIMEOUT = 120
TASK_BUDGET_USD = {"call_translate": 1.50, "call_summary": 1.50}
DEFAULT_BUDGET_USD = 0.50
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_SYSTEM = ("You are a precise assistant embedded in a financial data pipeline. Follow the user's format instructions exactly. "
                  "Output only the requested content with no preamble, no explanation, and no markdown code fences.")
JSON_SUFFIX = " Respond with valid JSON only."
FALLBACK_MARKERS = ("rate limit", "usage limit", "limit reached", "overloaded", "401", "403")
HAIKU_PRICE_IN, HAIKU_PRICE_OUT = 1.0 / 1_000_000, 5.0 / 1_000_000
KST = timezone(timedelta(hours=9))

logger = logging.getLogger("llm_client")
_log_lock = threading.Lock()
_sem = threading.Semaphore(max(1, int(os.environ.get("LLM_MAX_CONCURRENT", "2") or 2)))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BD, ".env"))
except Exception:
    pass


@dataclass
class LLMResult:
    text: str = ""
    data: object = None
    backend: str = ""
    model: str = ""
    tier: str = ""
    duration_ms: int = 0
    cost_usd: float = None
    input_tokens: int = None
    output_tokens: int = None
    cache_tokens: int = None
    fallback: bool = False
    fallback_reason: str = None
    error: str = None
    ok: bool = False
    num_turns: int = None
    task: str = ""
    extra: dict = field(default_factory=dict)


def _tier(task):
    t = TASK_TIERS.get(task)
    if t is None:
        logger.warning("llm_client: 등록되지 않은 task %r → haiku 로 처리", task)
        return "haiku"
    return t


def _cli_path():
    return os.environ.get("LLM_CLI_PATH") or "/Users/yun/.local/bin/claude"


def _cli_env():
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    if not env.get("HOME"):
        env["HOME"] = "/Users/yun"
    path = env.get("PATH", "")
    if "/Users/yun/.local/bin" not in path.split(":"):
        env["PATH"] = "/Users/yun/.local/bin" + (":" + path if path else "")
    return env


def _strip_fences(text):
    t = (text or "").strip()
    if t.startswith("```") and t.endswith("```"):
        t = t[3:]
        if t.lower().startswith("json"):
            t = t[4:]
        t = t[:-3].strip()
    return t


def _extract_json(text):
    try:
        return json.loads(text), None
    except Exception:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None, "json_parse_failed"
    i = min(starts)
    depth, in_str, esc, end = 0, False, False, -1
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end == -1:
        return None, "json_parse_failed"
    try:
        return json.loads(text[i:end + 1]), None
    except Exception:
        return None, "json_parse_failed"


def _needs_fallback(msg):
    m = (msg or "").lower()
    return any(k in m for k in FALLBACK_MARKERS)


def _run_cli(task, tier, prompt, system, timeout):
    res = LLMResult(backend="cli", tier=tier, task=task)
    exe = _cli_path()
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)) and not shutil.which(exe):
        res.error = f"cli_not_found:{exe}"
        return res, res.error
    budget = TASK_BUDGET_USD.get(task, DEFAULT_BUDGET_USD)
    args = [exe, "-p", "--model", tier, "--output-format", "json", "--system-prompt", system,
            "--tools", "", "--no-session-persistence", "--max-budget-usd", f"{budget:.2f}"]
    t0 = time.time()
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=timeout, env=_cli_env(), cwd=_BD)
    except subprocess.TimeoutExpired:
        res.duration_ms = int((time.time() - t0) * 1000)
        res.error = "timeout"
        return res, "timeout"
    except Exception as e:
        res.error = f"cli_exec_error:{type(e).__name__}"
        return res, res.error
    res.duration_ms = int((time.time() - t0) * 1000)
    out = proc.stdout or ""
    try:
        d = json.loads(out)
    except Exception:
        res.error = f"cli_bad_stdout:rc={proc.returncode}:{(proc.stderr or out)[:200].strip()}"
        return res, res.error
    usage = d.get("usage") or {}
    mu = d.get("modelUsage") or {}
    res.model = next(iter(mu.keys()), "") or ""
    res.cost_usd = d.get("total_cost_usd")
    res.input_tokens = usage.get("input_tokens")
    res.output_tokens = usage.get("output_tokens")
    res.cache_tokens = (usage.get("cache_creation_input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
    res.num_turns = d.get("num_turns")
    res.extra = {"subtype": d.get("subtype"), "stop_reason": d.get("stop_reason"), "rc": proc.returncode}
    result_text = d.get("result") if isinstance(d.get("result"), str) else json.dumps(d.get("result"), ensure_ascii=False) if d.get("result") is not None else ""
    subtype = str(d.get("subtype") or "")
    if "budget" in subtype.lower() or ("budget" in (result_text or "").lower() and d.get("is_error")):
        res.error = f"budget_exceeded:{subtype}"
        res.text = _strip_fences(result_text)
        return res, "budget_exceeded"
    if proc.returncode != 0 or d.get("is_error"):
        res.error = f"cli_error:rc={proc.returncode}:{subtype}:{(result_text or proc.stderr or '')[:200].strip()}"
        res.text = _strip_fences(result_text)
        return res, res.error
    if _needs_fallback(result_text) and len(result_text) < 400:
        res.error = f"cli_limit:{result_text[:200].strip()}"
        res.text = _strip_fences(result_text)
        return res, res.error
    res.text = _strip_fences(result_text)
    res.ok = True
    return res, None


def _run_api(task, tier, prompt, system, max_tokens, timeout, model=None):
    model = model or FALLBACK_MODEL
    res = LLMResult(backend="api", tier=tier, task=task, model=model)
    t0 = time.time()
    try:
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            res.error = "api_no_key"
            return res
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
        r = client.messages.create(model=model, max_tokens=max_tokens or 4096, system=system,
                                   messages=[{"role": "user", "content": prompt}])
        res.text = _strip_fences("".join(getattr(b, "text", "") for b in r.content))
        u = getattr(r, "usage", None)
        res.input_tokens = getattr(u, "input_tokens", None)
        res.output_tokens = getattr(u, "output_tokens", None)
        res.cache_tokens = (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        if res.input_tokens is not None and res.output_tokens is not None:
            res.cost_usd = round(res.input_tokens * HAIKU_PRICE_IN + res.output_tokens * HAIKU_PRICE_OUT, 6)
        res.num_turns = 1
        res.ok = True
    except Exception as e:
        res.error = f"api_error:{type(e).__name__}:{str(e)[:200]}"
    res.duration_ms = int((time.time() - t0) * 1000)
    return res


def _finish_json(res, json_mode):
    if not json_mode or not res.ok:
        return res
    data, err = _extract_json(res.text)
    if err:
        res.ok = False
        res.error = err
        res.data = None
    else:
        res.data = data
    return res


def _log(res, prompt_chars):
    row = {"ts": datetime.now(KST).isoformat(timespec="seconds"), "task": res.task, "tier": res.tier, "backend": res.backend,
           "model": res.model, "duration_ms": res.duration_ms, "cost_usd": res.cost_usd, "input_tokens": res.input_tokens,
           "output_tokens": res.output_tokens, "cache_tokens": res.cache_tokens, "fallback": res.fallback,
           "fallback_reason": res.fallback_reason, "error": res.error, "prompt_chars": prompt_chars, "ok": res.ok,
           "num_turns": res.num_turns}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("llm_client: 로그 쓰기 실패 %s", type(e).__name__)


def complete(task, prompt, system=None, json_mode=False, max_tokens=None, timeout=None):
    tier = _tier(task)
    sys_prompt = (system or DEFAULT_SYSTEM).rstrip()
    if json_mode:
        sys_prompt = sys_prompt + JSON_SUFFIX
    timeout = timeout or TASK_TIMEOUTS.get(task, DEFAULT_TIMEOUT)
    backend = (TASK_BACKEND.get(task) or os.environ.get("LLM_BACKEND") or "cli").lower()
    allow_fallback = (os.environ.get("LLM_API_FALLBACK") or "1") != "0"
    try:
        with _sem:
            if backend == "api":
                res = _run_api(task, tier, prompt, sys_prompt, max_tokens, timeout, model=API_MODELS.get(tier, FALLBACK_MODEL))
            else:
                res, reason = _run_cli(task, tier, prompt, sys_prompt, timeout)
                if not res.ok and reason and allow_fallback:
                    cli_err = res.error
                    api = _run_api(task, tier, prompt, sys_prompt, max_tokens, timeout)
                    api.fallback = True
                    api.fallback_reason = reason if reason in ("timeout", "budget_exceeded") else (cli_err or reason)
                    api.extra = {"cli_error": cli_err}
                    res = api
        res = _finish_json(res, json_mode)
    except Exception as e:
        res = LLMResult(backend=backend, tier=tier, task=task, error=f"unexpected:{type(e).__name__}:{str(e)[:200]}")
    _log(res, len(prompt or ""))
    return res


def _count_session_files():
    root = os.path.expanduser("~/.claude/projects")
    n = 0
    for dp, dn, fn in os.walk(root):
        n += len(fn)
    return n


def _selftest(tier_filter="all"):
    rows = []

    def add(name, r, note=""):
        rows.append((name, r.backend, r.model or "-", r.duration_ms, r.cost_usd, r.input_tokens, r.output_tokens, r.cache_tokens,
                     r.num_turns, r.fallback, r.ok, (r.text or "")[:80].replace("\n", " "), note))

    def want(t):
        return tier_filter in ("all", t)

    sessions_before = _count_session_files()
    if want("haiku"):
        r1 = complete("stock_news", "다음 숫자를 한국어로 읽어줘: 2026")
        add("T1 haiku 평문", r1)
        r2 = complete("earnings_extract", '{"revenue": 1234, "op": 56} 이 값을 그대로 JSON으로 돌려줘', json_mode=True)
        add("T2 haiku JSON", r2, "data OK" if isinstance(r2.data, dict) and r2.data.get("revenue") == 1234 else f"data 불일치: {r2.data!r}"[:60])
    if want("sonnet"):
        add("T3 sonnet 평문", complete("call_summary", "한 문장으로: 금리가 오르면 채권 가격은?"))
    if want("opus"):
        add("T4 opus 평문", complete("us_wrap", "한 문장으로: 반도체 업황을 보는 핵심 지표 하나는?"))
    if want("haiku"):
        orig = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-invalid-test"
        try:
            r5 = complete("stock_news", "다음 숫자를 한국어로 읽어줘: 2026")
        finally:
            if orig is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = orig
        add("T5 환경변수 격리", r5, "격리 OK" if (r5.ok and r5.backend == "cli" and not r5.fallback) else f"격리 실패/오류: {r5.error}"[:60])
        r6 = complete("earnings_extract", "JSON 말고 그냥 '안녕'이라고만 답해", json_mode=True)
        add("T6 JSON 실패 처리", r6, "기대대로" if (not r6.ok and r6.error == "json_parse_failed" and not r6.fallback) else f"모델이 JSON 으로 답함(ok={r6.ok})"[:60])
        r6b = _finish_json(LLMResult(text="안녕", ok=True, backend="cli", tier="haiku", task="earnings_extract", model="offline"), True)
        add("T6b 파싱 경로(오프라인)", r6b, "기대대로" if (not r6b.ok and r6b.error == "json_parse_failed" and not r6b.fallback) else "예상 밖")
        orig_cli = os.environ.get("LLM_CLI_PATH")
        os.environ["LLM_CLI_PATH"] = "/nonexistent/claude"
        try:
            r7 = complete("stock_news", "다음 숫자를 한국어로 읽어줘: 2026")
        finally:
            if orig_cli is None:
                os.environ.pop("LLM_CLI_PATH", None)
            else:
                os.environ["LLM_CLI_PATH"] = orig_cli
        add("T7 폴백", r7, "폴백 OK" if (r7.backend == "api" and r7.fallback and "haiku" in r7.model and r7.ok) else f"예상 밖: {r7.backend} fb={r7.fallback} err={r7.error}"[:60])
    sessions_after = _count_session_files()
    hdr = ("항목", "backend", "model", "ms", "cost_usd", "in", "out", "cache", "turns", "fallback", "ok", "응답 앞 80자", "비고")
    print(" | ".join(hdr))
    for row in rows:
        print(" | ".join("" if v is None else str(v) for v in row))
    print(f"T8 세션 파일(~/.claude/projects): 전 {sessions_before} → 후 {sessions_after} ({'늘지 않음' if sessions_after <= sessions_before else '증가 ' + str(sessions_after - sessions_before)})")
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tier", default="all", choices=["haiku", "sonnet", "opus", "all"])
    a = ap.parse_args()
    if a.selftest:
        logging.basicConfig(level=logging.WARNING)
        _selftest(a.tier)
    else:
        ap.print_help()
