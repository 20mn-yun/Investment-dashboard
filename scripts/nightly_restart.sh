#!/bin/sh
# 야간 재시작 (launchd: com.yun.nightly-restart, 매일 04:10)
# 1) dashboard 재시작 → 2) 20초 뒤 stockbot 재시작 → 3) Finder 재시작(열린 Finder 창 정리; Drive dataless 폴더 실체화용 open -g 가 남긴 창)
# set -e 는 쓰지 않음: Finder 가 떠 있지 않아 killall 이 실패해도 앞 단계는 그대로 둔다.
set -u

UID_NUM=$(id -u)
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "nightly_restart 시작"

launchctl kickstart -k "gui/${UID_NUM}/com.yun.dashboard"
log "dashboard kickstart rc=$?"

sleep 20

launchctl kickstart -k "gui/${UID_NUM}/com.yun.stockbot"
log "stockbot kickstart rc=$?"

killall Finder
log "Finder killall rc=$? (0=창 정리됨, 1=Finder 미실행)"

log "nightly_restart 끝"
