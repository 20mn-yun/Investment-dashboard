#!/bin/bash
OUT="$HOME/investment_dashboard/socket_baseline.txt"
UID_N=$(id -u)

resolve_pid() {
    local base cands c n best bestn
    base=$(launchctl print "gui/$UID_N/$1" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $NF; exit}')
    [ -z "$base" ] && return
    cands="$base $(pgrep -P "$base" 2>/dev/null | tr '\n' ' ')"
    best=""
    bestn=-1
    for c in $cands; do
        n=$(lsof -nP -i -a -p "$c" 2>/dev/null | awk 'NR>1' | wc -l | tr -d ' ')
        if [ "$n" -gt "$bestn" ]; then bestn=$n; best=$c; fi
    done
    echo "$best"
}

{
    echo "======================================================"
    date '+%Y-%m-%d %H:%M'
    echo "--- netstat states ---"
    netstat -an | awk '{print $6}' | sort | uniq -c | sort -rn | head -6
    echo "--- lsof per-process top10 ---"
    lsof -nP -i 2>/dev/null | awk 'NR>1{print $1, $2}' | sort | uniq -c | sort -rn | head -10
    DASH=$(resolve_pid com.yun.dashboard)
    BOT=$(resolve_pid com.yun.stockbot)
    echo "--- app.py(pid=${DASH:-none}) socket states ---"
    if [ -n "$DASH" ]; then lsof -nP -i -a -p "$DASH" 2>/dev/null | awk 'NR>1{print $10}' | sort | uniq -c; fi
    echo "--- stockbot(pid=${BOT:-none}) socket states ---"
    if [ -n "$BOT" ]; then lsof -nP -i -a -p "$BOT" 2>/dev/null | awk 'NR>1{print $10}' | sort | uniq -c; fi
    echo "--- CLOSE_WAIT total ---"
    netstat -an | grep -c CLOSE_WAIT
    EPH=$(netstat -an | awk '$4 ~ /\.(49[1-9][0-9][0-9]|5[0-9]{4}|6[0-5][0-9]{3})$/' | wc -l | tr -d ' ')
    PCT=$(awk "BEGIN{printf \"%.2f\", $EPH*100/16384}")
    echo "--- ephemeral usage: $EPH / 16384 = ${PCT}% ---"
} >> "$OUT"
