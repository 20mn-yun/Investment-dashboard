#!/usr/bin/env python3
"""대시보드 워치독 (launchd com.yun.watchdog, 2분마다 1회 실행).

app.py 와 독립: 표준 라이브러리만 쓰고 대시보드 코드를 import 하지 않는다 (대시보드가 죽어도 돌아야 함).
점검 3가지
  1) GET /healthz (10초) — 2회 연속 실패 → 진단 스냅샷 → launchctl kickstart -k → 🔴 알림.
     다음 점검 성공 → 🟢 복구 알림. 1시간 안 재시작 3회 초과 → 재시작 중단 + ⚠️ 알림(1시간 쿨다운).
  2) TIME_WAIT 수(netstat -an -p tcp) > 8,000 → ⚠️ 알림(6시간 쿨다운), 상위 목적지 3개 포함.
  3) 가동 40일 초과 → 주간 재부팅 예약(pmset repeat)이 안 돌고 있다는 알림(하루 1회).
옵션: --dry-run (재시작·발송 없이 할 일만 출력, 상태는 기록), --test-alert (테스트 메시지 1건)
환경변수: WATCHDOG_URL (기본 http://127.0.0.1:8000/healthz)
텔레그램: .env 의 TELEGRAM_TOKEN_GENERAL + dart_monitor_config.json 의 telegram_chat_ids.general (대시보드와 같은 경로)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE, ".env")
CFG_PATH = os.path.join(BASE, "dart_monitor_config.json")
APP_LOG = os.path.join(BASE, "app.log")                     # dashboard plist 의 StandardOut/ErrorPath
STATE_PATH = os.path.join(BASE, "cache", "watchdog_state.json")
LOG_PATH = os.path.join(BASE, "logs", "watchdog.log")
INCIDENT_DIR = os.path.join(BASE, "logs", "watchdog_incidents")
LABEL = "com.yun.dashboard"

URL = os.environ.get("WATCHDOG_URL") or "http://127.0.0.1:8000/healthz"
HTTP_TIMEOUT = 10
FAILS_TO_RESTART = 2
MAX_RESTARTS_PER_HOUR = 3
TIME_WAIT_WARN = 8000
TIME_WAIT_COOLDOWN = 6 * 3600
REPEAT_COOLDOWN = 3600
UPTIME_WARN_DAYS = 40
UPTIME_COOLDOWN = 24 * 3600
HEARTBEAT_SEC = 3600
INCIDENTS_KEEP = 30

DRY = "--dry-run" in sys.argv


# ---------- 공통 ----------
def now_s():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now_s()}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"상태 저장 실패: {type(e).__name__}")


def run(cmd, timeout=20):
    """명령 실행 → stdout 문자열 (실패 시 '')"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------- 텔레그램 (토큰·키는 로그에 남기지 않음) ----------
def _read_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def tg_creds():
    token = os.environ.get("TELEGRAM_TOKEN_GENERAL") or _read_env(ENV_PATH).get("TELEGRAM_TOKEN_GENERAL")
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            chat_id = (json.load(f).get("telegram_chat_ids") or {}).get("general")
    except (OSError, ValueError):
        chat_id = None
    return (token, chat_id) if token and chat_id else (None, None)


def tg_send(text):
    """발송 성공 여부. 실패(포트 고갈 등)해도 예외 없이 False + 로그 한 줄."""
    if DRY:
        log(f"[dry-run] 텔레그램 발송 예정:\n{text}")
        return True
    token, chat_id = tg_creds()
    if not token or not chat_id:
        log("텔레그램 설정 없음(.env TELEGRAM_TOKEN_GENERAL / dart_monitor_config general) — 발송 생략")
        return False
    body = json.dumps({"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception as e:   # 주소에 토큰이 있으므로 예외 메시지는 남기지 않는다
        log(f"텔레그램 발송 실패: {type(e).__name__}")
        return False
    if not ok:
        log("텔레그램 발송 실패: 응답 ok=false")
    return ok


# ---------- 시스템 관측 ----------
def health_check():
    """(ok, 소요초, 사유)"""
    t0 = time.time()
    try:
        with urllib.request.urlopen(URL, timeout=HTTP_TIMEOUT) as r:
            body = r.read(4096).decode("utf-8", "replace")
            if r.status == 200 and '"ok"' in body:
                return True, time.time() - t0, ""
            return False, time.time() - t0, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, time.time() - t0, f"HTTP {e.code}"
    except Exception as e:
        return False, time.time() - t0, type(e).__name__


def tcp_states():
    """(상태별 개수 Counter, TIME_WAIT 목적지 IP Counter)"""
    states, tw_dst = Counter(), Counter()
    for line in run(["netstat", "-an", "-p", "tcp"]).splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("tcp"):
            continue
        st = parts[-1]
        states[st] += 1
        if st == "TIME_WAIT":
            m = re.match(r"^(.*)\.(\d+|\*)$", parts[4])   # 외부 주소 "1.2.3.4.443" → IP
            tw_dst[m.group(1) if m else parts[4]] += 1
    return states, tw_dst


def uptime_days():
    m = re.search(r"sec\s*=\s*(\d+)", run(["sysctl", "-n", "kern.boottime"]))
    return (time.time() - int(m.group(1))) / 86400 if m else None


def tail_lines(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return [f"(읽기 실패: {path})"]


def snapshot(reason):
    """진단 스냅샷 저장 → 파일 경로. 최근 INCIDENTS_KEEP 개만 보관."""
    states, tw_dst = tcp_states()
    up = uptime_days()
    L = [f"# watchdog incident {now_s()} — {reason}", f"url: {URL}", "",
         f"## uptime: {up:.1f}일" if up is not None else "## uptime: ?", run(["uptime"]).strip(), "",
         "## TCP 상태별 개수"] + [f"{k:14} {v}" for k, v in states.most_common()] + ["",
         "## TIME_WAIT 상위 목적지 10"] + [f"{ip:40} {n}" for ip, n in tw_dst.most_common(10)] + ["",
         "## vm_stat", run(["vm_stat"]).strip(), "",
         f"## 대시보드 stderr/stdout 마지막 80줄 ({APP_LOG})"] + tail_lines(APP_LOG, 80)
    try:
        os.makedirs(INCIDENT_DIR, exist_ok=True)
        path = os.path.join(INCIDENT_DIR, datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        old = sorted(x for x in os.listdir(INCIDENT_DIR) if x.endswith(".log"))
        for x in old[:-INCIDENTS_KEEP]:
            os.remove(os.path.join(INCIDENT_DIR, x))
        return path
    except OSError as e:
        log(f"스냅샷 저장 실패: {type(e).__name__}")
        return None


def restart_dashboard():
    cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]
    if DRY:
        log(f"[dry-run] 재시작 예정: {' '.join(cmd)}")
        return True
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        log(f"kickstart rc={r.returncode} {(r.stderr or '').strip()[:200]}")
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log(f"kickstart 실패: {type(e).__name__}")
        return False


# ---------- 점검 ----------
def check_health(st, now):
    ok, sec, why = health_check()
    if ok:
        if st.get("down_since") and (st.get("restarted") or st.get("fails", 0) >= FAILS_TO_RESTART):
            mins = max(1, round((now - st["down_since"]) / 60))
            log(f"복구 확인: {URL} {sec:.2f}s (중단 약 {mins}분)")
            tg_send(f"🟢 대시보드 복구됨 (중단 약 {mins}분)\n{now_s()}")
        elif st.get("fails"):
            log(f"일시 실패 뒤 정상: {sec:.2f}s")
        st["fails"], st["down_since"], st["restarted"] = 0, None, False
        return True
    st["fails"] = st.get("fails", 0) + 1
    if not st.get("down_since"):
        st["down_since"] = now
    log(f"healthz 실패 {st['fails']}회: {why} ({sec:.1f}s)")
    if st["fails"] < FAILS_TO_RESTART:
        return False
    # 재시작 뒤에도 계속 실패하면 아래 횟수 제한(1시간 3회) 안에서 다시 시도
    recent = [t for t in st.get("restarts", []) if now - t < 3600]
    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        if now - st.get("last_repeat_alert", 0) >= REPEAT_COOLDOWN:
            log(f"1시간 안 재시작 {len(recent)}회 → 더 이상 재시작하지 않음")
            tg_send(f"⚠️ 대시보드 반복 실패, 수동 확인 필요\n1시간 안 자동 재시작 {len(recent)}회에도 /healthz 무응답({why}). 재시작을 멈춥니다.\n{now_s()}")
            st["last_repeat_alert"] = now
        return False
    path = snapshot(f"healthz {st['fails']}회 연속 실패: {why}")
    log(f"{st['fails']}회 연속 실패 → 재시작 (스냅샷 {path})")
    if restart_dashboard():
        st["restarts"] = recent + [now]
        st["restarted"] = True
        tg_send(f"🔴 대시보드 무응답 → 자동 재시작\n사유: {why}, 연속 {st['fails']}회\n스냅샷: {os.path.basename(path) if path else '없음'}\n{now_s()}")
    return False


def check_time_wait(st, now):
    states, tw_dst = tcp_states()
    tw = states.get("TIME_WAIT", 0)
    if tw <= TIME_WAIT_WARN:
        return tw
    if now - st.get("last_tw_alert", 0) < TIME_WAIT_COOLDOWN:
        log(f"TIME_WAIT {tw} (쿨다운 중)")
        return tw
    top = ", ".join(f"{ip} {n}" for ip, n in tw_dst.most_common(3))
    log(f"TIME_WAIT {tw} > {TIME_WAIT_WARN} → 경고 (상위: {top})")
    tg_send(f"⚠️ TIME_WAIT {tw:,}개 (기준 {TIME_WAIT_WARN:,})\n상위 목적지: {top}\nESTABLISHED {states.get('ESTABLISHED', 0)}, CLOSE_WAIT {states.get('CLOSE_WAIT', 0)}\n"
            f"가동 {uptime_days() or 0:.1f}일. 49.7일 전 재부팅 필요.\n{now_s()}")
    st["last_tw_alert"] = now
    return tw


def check_uptime(st, now):
    up = uptime_days()
    if up is None or up <= UPTIME_WARN_DAYS:
        return up
    if now - st.get("last_uptime_alert", 0) >= UPTIME_COOLDOWN:
        log(f"가동 {up:.1f}일 > {UPTIME_WARN_DAYS}일 → 경고")
        tg_send(f"⚠️ 맥미니 가동 {up:.1f}일 — 주간 재부팅 예약이 안 돌고 있음\n49.7일 넘으면 TCP 포트 고갈(10/30 장애). 확인: pmset -g sched / 수동: sudo shutdown -r now\n{now_s()}")
        st["last_uptime_alert"] = now
    return up


def main():
    if "--test-alert" in sys.argv:
        ok = tg_send(f"🧪 워치독 테스트 메시지\n{URL}\n{now_s()}")
        print("발송", "성공" if ok else "실패")
        return 0 if ok else 1
    now = time.time()
    st = load_state()
    healthy = check_health(st, now)
    tw = check_time_wait(st, now)
    up = check_uptime(st, now)
    if healthy and now - st.get("last_heartbeat", 0) >= HEARTBEAT_SEC:
        log(f"heartbeat ok — TIME_WAIT {tw}, 가동 {up:.1f}일" if up is not None else f"heartbeat ok — TIME_WAIT {tw}")
        st["last_heartbeat"] = now
    save_state(st)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
