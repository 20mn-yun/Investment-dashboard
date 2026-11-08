#!/bin/sh
# 야간 재시작 (launchd: com.yun.nightly-restart, 매일 04:10)
# 1) 로그 로테이션 mv 단계(20MB 이상만 .1 로) → 2) dashboard 재시작
# → 3) Finder 재시작(열린 Finder 창 정리; Drive dataless 폴더 실체화용 open -g 가 남긴 창) → 4) 로테이션 gzip 단계(재시작을 막지 않게 맨 뒤)
# stockbot(stock_info_bot)은 2026-11-08 영구 중지(launchd/disabled) — 재시작 대상 아님
# launchd 는 kickstart 때 StandardOutPath 를 다시 열므로 mv 뒤 재시작이면 새 로그 파일로 이어진다.
# set -e 는 쓰지 않음: Finder 가 떠 있지 않아 killall 이 실패해도 앞 단계는 그대로 둔다.
set -u

UID_NUM=$(id -u)
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "nightly_restart 시작"

sh "$SCRIPT_DIR/rotate_logs.sh" mv
log "rotate mv rc=$?"

launchctl kickstart -k "gui/${UID_NUM}/com.yun.dashboard"
log "dashboard kickstart rc=$?"

killall Finder
log "Finder killall rc=$? (0=창 정리됨, 1=Finder 미실행)"

sh "$SCRIPT_DIR/rotate_logs.sh" gzip
log "rotate gzip rc=$?"

log "nightly_restart 끝"
