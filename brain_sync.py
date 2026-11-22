"""브레인 동기화 (05:30 미리 받기 끝에 자동 실행, 수동: python3 brain_sync.py).

① tools/brain_export.py (대시보드 자료 → ~/investment_brain 마크다운)
② gbrain import ~/investment_brain (바뀐 페이지만)
③ gbrain embed --stale --catch-up (import 가 일부 청크를 대기로 남기므로 — bge-m3, 로컬 Ollama)
④ ~/investment_brain git add/commit(변경 있을 때만)/push (실패해도 진행)
단계별 소요·건수·오류를 logs/brain_sync.log 에 한 줄씩. 어느 단계가 실패해도 예외를 밖으로 던지지 않는다.
gbrain 실행 규칙은 mcp_server._gbrain 과 같다(HOME, PATH 에 ~/.bun/bin·~/.local/bin, cwd ~/investment_brain). pglite_busy 면 20초 뒤 1회 재시도.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

DASH = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = "/Users/yun"
BRAIN_DIR = os.path.join(HOME_DIR, "investment_brain")
LOG_PATH = os.path.join(DASH, "logs", "brain_sync.log")
GBRAIN = os.environ.get("GBRAIN_BIN") or os.path.join(HOME_DIR, ".bun", "bin", "gbrain")
_MASK = [re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}"), re.compile(r"sk-[A-Za-z0-9_-]{16,}"), re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])")]


def _mask(s):
    s = s or ""
    for rx in _MASK:
        s = rx.sub("***", s)
    return re.sub(r"(?i)\b(key|token|secret)=[^&\s\"']+", r"\1=***", s)


def _env():
    env = dict(os.environ)
    env["HOME"] = HOME_DIR
    env["PATH"] = f"{HOME_DIR}/.bun/bin:{HOME_DIR}/.local/bin:" + env.get("PATH", "")
    return env


def _run(args, cwd, timeout):
    """반환 (rc, stdout, stderr). 예외 없음."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_env())
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout}s"
    except OSError as e:
        return -1, "", f"{type(e).__name__}: {e}"


def _gbrain(args, timeout):
    """gbrain 1회 + pglite_busy 면 20초 뒤 1회 재시도. 반환 (rc, out, err)"""
    rc, out, err = _run([GBRAIN, *args], BRAIN_DIR, timeout)
    if rc != 0 and re.search(r"pglite_busy|already open|locked", err + out, re.I):
        time.sleep(20)
        rc, out, err = _run([GBRAIN, *args], BRAIN_DIR, timeout)
    return rc, out, err


def _clean_err(err):
    lines = [l for l in (err or "").splitlines() if l.strip() and not l.startswith("[models]")]
    return _mask(" / ".join(lines))[:300]


def _log(line):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def run(reason="batch"):
    t_all = time.time()
    res = {"reason": reason, "started_at": datetime.now().isoformat(timespec="seconds"), "steps": {}}

    def step(name, fn):
        t0 = time.time()
        try:
            out = fn()
        except Exception as e:      # 어떤 단계도 밖으로 예외를 던지지 않음
            out = {"ok": False, "error": f"{type(e).__name__}: {_mask(str(e))[:200]}"}
        out["sec"] = round(time.time() - t0, 1)
        res["steps"][name] = out
        _log(f"[{reason}] {name} " + json.dumps(out, ensure_ascii=False))
        return out

    # ① 내보내기
    def export():
        rc, out, err = _run([sys.executable, os.path.join("tools", "brain_export.py")], DASH, 300)
        if rc != 0:
            return {"ok": False, "error": _clean_err(err) or f"rc={rc}"}
        try:
            d = json.loads(out[out.find("{"):])
        except ValueError:
            return {"ok": False, "error": "brain_export 출력 해석 실패"}
        c = d.get("counts") or {}
        return {"ok": not d.get("failures"), "changed_files": d.get("changed_files"), "companies": c.get("companies"),
                "targets": (c.get("targets_watchlist") or 0) + (c.get("targets_peer") or 0), "failures": len(d.get("failures") or [])}
    step("export", export)

    # ② import
    def do_import():
        rc, out, err = _gbrain(["import", BRAIN_DIR], 300)
        m = re.search(r"(\d+) pages imported", out)
        ch = re.search(r"(\d+) chunks created", out)
        r = {"ok": rc == 0, "pages_imported": int(m.group(1)) if m else None, "chunks_created": int(ch.group(1)) if ch else None}
        if rc != 0:
            r["error"] = _clean_err(err or out)
        return r
    step("import", do_import)

    # ③ 임베딩 (대기 청크)
    def do_embed():
        rc, out, err = _gbrain(["embed", "--stale", "--catch-up"], 1800)
        m = re.search(r"Embedded (\d+) chunks across (\d+) pages", out + err)
        r = {"ok": rc == 0, "embedded": int(m.group(1)) if m else 0, "pages": int(m.group(2)) if m else 0}
        if rc != 0:
            r["error"] = _clean_err(err or out)
        return r
    step("embed", do_embed)

    # ④ git
    def do_git():
        rc, _, err = _run(["git", "add", "companies", "sources", "analysis"], BRAIN_DIR, 60)
        if rc != 0:
            return {"ok": False, "error": _clean_err(err)}
        rc, _, _ = _run(["git", "diff", "--cached", "--quiet"], BRAIN_DIR, 60)
        if rc == 0:
            return {"ok": True, "committed": False}
        msg = f"sync {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        rc, out, err = _run(["git", "commit", "-q", "-m", msg], BRAIN_DIR, 60)
        if rc != 0:
            return {"ok": False, "committed": False, "error": _clean_err(err or out)}
        _, sha, _ = _run(["git", "rev-parse", "--short", "HEAD"], BRAIN_DIR, 30)
        prc, pout, perr = _run(["git", "push", "-q"], BRAIN_DIR, 60)
        return {"ok": True, "committed": True, "commit": sha.strip(), "message": msg, "pushed": prc == 0,
                **({"push_error": _clean_err(perr or pout)} if prc != 0 else {})}
    step("git", do_git)

    res["sec"] = round(time.time() - t_all, 1)
    res["ok"] = all(s.get("ok") for s in res["steps"].values())
    st = res["steps"]
    res["summary"] = (f"내보내기 변경 {st['export'].get('changed_files', '-')}·대상 {st['export'].get('targets', '-')} / "
                      f"import {st['import'].get('pages_imported', '-')}p / 임베딩 {st['embed'].get('embedded', '-')} / "
                      + ("커밋 " + str(st["git"].get("commit")) + (" push" if st["git"].get("pushed") else " push실패") if st["git"].get("committed") else "커밋 없음")
                      + f" / {res['sec']}s" + ("" if res["ok"] else " (일부 실패)"))
    _log(f"[{reason}] done {res['summary']}")
    return res


if __name__ == "__main__":
    print(json.dumps(run(reason="manual"), ensure_ascii=False, indent=1))
