#!/bin/sh
# 로그 로테이션 (nightly_restart.sh 에서 호출). 사용: rotate_logs.sh [mv|gzip]  (인자 없으면 mv → gzip)
#  mv   : THRESHOLD_MB(기본 20) 이상인 파일만 .1 로 옮기고 기존 .1→.2 … .5 를 밀어 올림(.6 은 삭제). 같은 폴더.
#  gzip : .2 이상 중 아직 압축 안 된 것을 gzip (순차 실행. stockbot 400MB 는 수 분 걸릴 수 있어 재시작 뒤에 돌린다)
#  nightly_restart.log(자기 자신의 stdout)는 마지막에, 크기 조건만 같다.
set -u

THRESHOLD_MB=${THRESHOLD_MB:-20}
HOME_DIR=${HOME:-/Users/yun}
DASH=/Users/yun/investment_dashboard
FILES="$HOME_DIR/Library/Logs/stockbot.log
$HOME_DIR/Library/Logs/stockbot_error.log
$DASH/app.log
$DASH/logs/mcp_server.launchd.log
$DASH/nightly_restart.log"

size_mb() { # 정수 MB (내림)
    sz=$(stat -f %z "$1" 2>/dev/null || echo 0)
    echo $((sz / 1048576))
}

do_mv() {
    echo "$FILES" | while IFS= read -r f; do
        [ -n "$f" ] || continue
        if [ ! -f "$f" ]; then
            echo "[rotate] $f 없음"
            continue
        fi
        mb=$(size_mb "$f")
        if [ "$mb" -lt "$THRESHOLD_MB" ]; then
            echo "[rotate] $f 생략(${mb}MB < ${THRESHOLD_MB})"
            continue
        fi
        rm -f "$f.6" "$f.6.gz"
        i=5
        while [ "$i" -ge 1 ]; do
            n=$((i + 1))
            [ -f "$f.$i" ] && mv -f "$f.$i" "$f.$n"
            [ -f "$f.$i.gz" ] && mv -f "$f.$i.gz" "$f.$n.gz"
            i=$((i - 1))
        done
        rm -f "$f.6" "$f.6.gz"
        if mv -f "$f" "$f.1"; then
            echo "[rotate] $f ${mb}MB → .1"
        else
            echo "[rotate] $f mv 실패"
        fi
    done
}

do_gzip() {
    echo "$FILES" | while IFS= read -r f; do
        [ -n "$f" ] || continue
        i=2
        while [ "$i" -le 5 ]; do
            if [ -f "$f.$i" ]; then
                start=$(date +%s)
                if gzip -f "$f.$i"; then
                    echo "[gzip] $f.$i → .gz ($(( $(date +%s) - start ))초)"
                else
                    echo "[gzip] $f.$i 실패"
                fi
            fi
            i=$((i + 1))
        done
    done
}

case "${1:-all}" in
    mv) do_mv ;;
    gzip) do_gzip ;;
    all) do_mv; do_gzip ;;
    *) echo "사용법: $0 [mv|gzip]" >&2; exit 2 ;;
esac
