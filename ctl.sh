#!/bin/bash
# 컨트롤러(6655) 켜기/끄기/상태.  ./ctl.sh start [RUN_DIR] [EPISODE] [SCENARIO] | stop | status
# SCENARIO 는 A/B/C. C 면 우회 경로 meter 가 6 Mbps 로 깔린다 (surge.py 에도 같은 --scenario 를 줄 것)
cd "$(dirname "$0")"
PAT='ryu-manager --ofp-tcp-listen-port 6655'
case "${1:-status}" in
  start)
    pgrep -f "$PAT" >/dev/null && { echo "이미 실행 중"; exit 1; }
    export RUN_DIR="${2:-runs/manual}" EPISODE="${3:-1}" SCENARIO="${4:-${SCENARIO:-A}}"
    mkdir -p "$RUN_DIR/logs"
    LOG="$RUN_DIR/logs/ep_${EPISODE}_ryu.log"
    nohup ../ryu-venv/bin/ryu-manager --ofp-tcp-listen-port 6655 controller.py monitor.py > "$LOG" 2>&1 &
    sleep 2; python3 apply.py --reconnect >/dev/null
    for i in $(seq 1 30); do n=$(grep -o '\[연결\] S[1-4]' "$LOG" | sort -u | wc -l); [ "$n" -ge 4 ] && break; sleep 1; done
    echo "컨트롤러 기동 (시나리오 $SCENARIO). 붙은 스위치 $n/4. 로그: $LOG" ;;
  stop)  pkill -f "$PAT" && echo "종료" || echo "실행 중 아님" ;;
  status) if pgrep -f "$PAT" >/dev/null; then pgrep -af "$PAT" | cut -c1-90; else echo "실행 중 아님"; fi ;;
esac
