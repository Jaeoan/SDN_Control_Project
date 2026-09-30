#!/bin/bash
# 회차 반복 실행 (GNS3 판, sudo 불필요).  ./run.sh <run_name> <회차수> [--mock] [--no-advisor]
#   --mock        advisor 를 API 없이 규칙 기반으로 (루프 시험, LLM 아님)
#   --claude-code advisor 가 Claude Code 명령줄(Pro 구독)로 LLM 을 부른다. API 키 불필요
#   --no-advisor  파라미터를 바꾸지 않고 반복만 ("안 고쳤을 때" 기준선)
#   --from K      K 회차부터 이어서 (중간에 끊겼을 때. params.yaml 은 끊기기 직전 값 그대로 쓴다)
#   SURGE_ARGS="--ramp 0" ./run.sh ...   surge.py 에 넘길 추가 인자 (예: 계단형 폭주)
#   SCENARIOS="A A A A A B B B B B C C C C C" ./run.sh ...   회차별 폭주 시나리오 (모자라면 마지막 것을 반복, 기본 A)
# 전제: GNS3 노드가 켜져 있고 apply.py 를 한 번 돌린 상태 (python3 gns3_build.py start && python3 apply.py)
# 회차마다: 컨트롤러+모니터 기동 -> 스위치 재접속 -> surge.py -> 컨트롤러 종료 -> 그래프 -> advisor
set -u
cd "$(dirname "$0")"
NAME="${1:?사용법: ./run.sh <run_name> <회차수> [--mock] [--no-advisor]}"
N="${2:?회차수}"
shift 2
ADV_ARGS=""; ADVISOR=1; FROM=1
while [ $# -gt 0 ]; do
  case "$1" in --mock) ADV_ARGS="--mock";; --claude-code) ADV_ARGS="--claude-code";; --no-advisor) ADVISOR=0;; --from) FROM="$2"; shift;; esac
  shift
done
read -r -a SC_LIST <<< "${SCENARIOS:-A}"
sc_of() { local i=$(( $1 - 1 )); [ $i -ge ${#SC_LIST[@]} ] && i=$(( ${#SC_LIST[@]} - 1 )); echo "${SC_LIST[$i]}"; }
VENV="${VENV:-../ryu-venv}"           # ryu 가 깔린 venv
PY="$VENV/bin/python3"
SYS_PY="${SYS_PY:-/usr/bin/python3}"  # matplotlib 가 있는 시스템 파이썬
PORT=6655
export RUN_DIR="runs/$NAME"
mkdir -p "$RUN_DIR/logs"

if pgrep -f "ryu-manager .*$PORT" >/dev/null; then
  echo "이미 $PORT 포트에 ryu-manager 가 떠 있다. 먼저 끌 것: pkill -f 'ryu-manager .*$PORT'"; exit 1
fi
python3 gns3_build.py status | grep -qE "컨테이너 없음|닫혀" && { echo "노드가 준비되지 않았다. python3 gns3_build.py start && python3 apply.py"; exit 1; }

RYU_PID=""
cleanup_all() { [ -n "$RYU_PID" ] && kill "$RYU_PID" 2>/dev/null; }
trap cleanup_all EXIT

for ((ep=FROM; ep<=N; ep++)); do
  export EPISODE=$ep SCENARIO=$(sc_of $ep)
  echo "=============== 회차 $ep / $N  시나리오 $SCENARIO  ($(date +%H:%M:%S))  $(grep -E '^(threshold|split_ratio|auto)' params.yaml | awk '{print $1,$2}' | tr "\n" " ")"
  "$VENV/bin/ryu-manager" --ofp-tcp-listen-port $PORT controller.py monitor.py \
      > "$RUN_DIR/logs/ep_${ep}_ryu.log" 2>&1 &
  RYU_PID=$!
  sleep 2
  python3 apply.py --reconnect > /dev/null || { echo "apply 실패"; exit 1; }
  connected() { grep -o '\[연결\] S[1-4]' "$RUN_DIR/logs/ep_${ep}_ryu.log" | sort -u | wc -l; }   # 서로 다른 스위치 수
  for i in $(seq 1 30); do [ "$(connected)" -ge 4 ] && break; sleep 1; done
  if [ "$(connected)" -lt 4 ]; then
    echo "스위치 4대가 붙지 않았다. $RUN_DIR/logs/ep_${ep}_ryu.log 확인"; exit 1
  fi
  sleep 12                                   # 통계 기준선 한두 줄
  python3 surge.py --episode $ep ${SURGE_ARGS:-} | tee "$RUN_DIR/logs/ep_${ep}_surge.log"
  sleep 11                                   # 마지막 통계 한 줄 더
  kill "$RYU_PID"; wait "$RYU_PID" 2>/dev/null; RYU_PID=""
  "$SYS_PY" plot.py timeline "$RUN_DIR" $ep 2>/dev/null
  if [ $ADVISOR = 1 ]; then
    "$PY" advisor.py --episode $ep --run-dir "$RUN_DIR" $ADV_ARGS
  else
    "$PY" - "$RUN_DIR" $ep <<'PYEOF'
import sys; sys.path.insert(0, '.'); import common, advisor
rd, ep = sys.argv[1], int(sys.argv[2])
h = [x for x in (common.load_json(common.history_json(rd), []) or []) if x['episode'] != ep]
s = advisor.summarize(ep, rd); s['action'] = 'baseline'; s['applied'] = dict(s['params'])
h.append(s); common.save_json(common.history_json(rd), h)
print('[기준선] ep%d 손실률 %s%% (파라미터 유지)' % (ep, s['loss_pct']))
PYEOF
  fi
done
"$SYS_PY" plot.py summary "$RUN_DIR" 2>/dev/null
echo "끝. 결과: $RUN_DIR/"
