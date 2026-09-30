#!/usr/bin/env python3
"""시나리오가 계산대로 재현되는지 확인한다 (CHANGES.md 20번의 "숫자 먼저 확인").

  python3 verify_scenarios.py            # 기본 조합 7개
  python3 verify_scenarios.py A:50 C:40  # 원하는 조합만

조합마다: 컨트롤러를 그 시나리오로 띄움 -> threshold 80 / split_ratio 지정 -> 짧은 폭주 -> 최고치 구간 손실률을 예측과 비교.
예측 = (각 경로에서 가용 대역을 넘는 양의 합) / 전체 송신량. 패킷 헤더 때문에 링크 부하는 송신량의 1442/1400 배.
결과는 runs/verify/ 에 남고, 표로 출력된다. 차이가 1.5%p 를 넘으면 FAIL.
"""
import json
import os
import subprocess
import sys

import common

WIRE = 1442 / 1400
DEFAULT = ['A:20', 'A:50', 'B:30', 'B:50', 'C:30', 'C:40', 'C:50']


def predict(sc, sr):
    s = common.SCENARIOS[sc]
    n = common.detour_hosts(sr)
    det = sum(s['high'][h] for h in common.HOSTS[:n]) * WIRE
    dft = sum(s['high'][h] for h in common.HOSTS[n:]) * WIRE
    over = max(0, det - s['detour_capacity']) + max(0, dft - common.CAPACITY_MBPS)
    return round(over / (det + dft) * 100, 1), round(det, 2), round(dft, 2)


def main():
    combos = sys.argv[1:] or DEFAULT
    orig = open(common.PARAMS_FILE).read()
    rows = []
    try:
        for i, c in enumerate(combos, 1):
            sc, sr = c.split(':')
            sr = int(sr)
            p = common.load_params()
            p.update(threshold=80, split_ratio=sr, auto=True)
            common.save_params(p)
            env = dict(os.environ, RUN_DIR='runs/verify', EPISODE=str(i), SCENARIO=sc)
            subprocess.run(['./ctl.sh', 'stop'], capture_output=True)
            out = subprocess.run(['./ctl.sh', 'start', 'runs/verify', str(i), sc], env=env,
                                 capture_output=True, text=True).stdout.strip()
            if '4/4' not in out:
                sys.exit('컨트롤러 연결 실패: %s' % out)
            r = subprocess.run(['python3', 'surge.py', '--episode', str(i), '--scenario', sc,
                                '--warm', '5', '--ramp', '12', '--surge', '30', '--cool', '3'],
                               env=env, capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit('surge 실패:\n' + r.stdout[-800:] + r.stderr[-800:])
            res = json.load(open(common.result_json(i, 'runs/verify')))
            pred, det, dft = predict(sc, sr)
            got = res['peak_loss_pct']
            ok = abs(got - pred) <= 1.5
            rows.append((sc, sr, det, dft, pred, got, ok))
            print('%s split %3d  우회 %5.2f / 기본 %5.2f Mbps  예측 %5.1f%%  측정 %5.1f%%  %s' % (
                sc, sr, det, dft, pred, got, 'OK' if ok else 'FAIL'), flush=True)
    finally:
        subprocess.run(['./ctl.sh', 'stop'], capture_output=True)
        open(common.PARAMS_FILE, 'w').write(orig)
    common.save_json('runs/verify/summary.json', [dict(zip(
        ['scenario', 'split_ratio', 'detour_mbps', 'default_mbps', 'predicted', 'measured', 'ok'], r)) for r in rows])
    bad = [r for r in rows if not r[-1]]
    print('\n%d/%d 통과' % (len(rows) - len(bad), len(rows)))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
