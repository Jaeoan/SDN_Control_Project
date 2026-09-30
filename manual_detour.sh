#!/bin/bash
# 컨트롤러에 수동으로 우회로를 열고 닫으라고 지시한다 (params.yaml 의 auto 가 false 일 때 쓴다).
#   ./manual_detour.sh 50      -> 50% (5대 중 3대) 를 우회로로
#   ./manual_detour.sh close   -> 우회 해제
# RUN_DIR 는 컨트롤러를 띄울 때 준 값과 같아야 한다 (기본 runs/manual).
set -eu
cd "$(dirname "$0")"
RUN_DIR="${RUN_DIR:-runs/manual}"
[ $# -ge 1 ] || { echo "사용법: RUN_DIR=runs/manual $0 <split_ratio|close>"; exit 1; }
mkdir -p "$RUN_DIR"
if [ "$1" = "close" ]; then echo "close" > "$RUN_DIR/detour_cmd"; else echo "open $1" > "$RUN_DIR/detour_cmd"; fi
echo "명령 기록: $RUN_DIR/detour_cmd  (컨트롤러가 1초 안에 읽어 지운다)"
