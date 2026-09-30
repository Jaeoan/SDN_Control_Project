#!/usr/bin/env python3
"""급증형 폭주 재생 (GNS3 판, sudo 불필요). h1~h10 컨테이너에서 srv 로 iperf3 UDP.

  warm(낮게) -> ramp(서서히 올림) -> surge(최고치 유지) -> cool(다시 낮게)
  기본: 30초 x 호스트당 0.25 Mbps(합 2.5), 60초 동안 12단계로 상승, 120초 최고치, 20초 낮게.
  최고치는 시나리오(--scenario A/B/C, common.SCENARIOS)가 호스트별로 정한다.
    A 균등형   : 10대 x 1.8 Mbps = 18 Mbps
    B 쏠림형   : h1~h3 x 3 Mbps + 나머지 7대 합 8 Mbps = 17 Mbps
    C 우회로 좁음: A 와 같은 트래픽, 우회 경로 대역 6 Mbps (controller.py 가 meter 로)
  --ramp 0 을 주면 예전처럼 한순간에 뛰는 계단형이 된다.
  점증형을 기본으로 한 이유: 실제 수강신청 폭주는 정각 전부터 접속이 점점 늘어난다. 계단형이면 threshold 가
  얼마든 1초 뒤에 열려서 threshold 를 배울 게 없지만, 점증형이면 threshold 가 낮을수록 더 일찍 열린다.

끝나면 RUN_DIR/stats/ep_N_result.json 에 단계별 시각, 호스트별 lost/total, 폭주 구간 손실률을 남긴다.
손실률은 iperf3 의 서버 쪽 보고(end.sum.lost_packets / packets).
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

BASE_PORT = 5201


def start_servers():
    ports = ' '.join(str(BASE_PORT + i) for i in range(len(common.HOSTS)))
    common.dexec('srv', ['sh', '-c', 'killall iperf3 2>/dev/null; sleep 1; for p in %s; do iperf3 -s -p $p -D; done' % ports])
    for _ in range(10):                      # 서버 5개가 전부 듣기 시작할 때까지 기다린다
        time.sleep(1)
        n = common.dexec('srv', ['sh', '-c', 'netstat -ltn 2>/dev/null | grep -c ":52"']).stdout.strip()
        if int(n or 0) >= len(common.HOSTS):
            break
    else:
        sys.exit('srv 의 iperf3 서버 기동 실패 (%s개)' % n)
    # 호스트 -> srv 가 실제로 통하는지 확인 (ARP 예열도 겸한다). 안 통하면 회차를 시작하지 않는다
    dead = [h for h in common.HOSTS
            if common.dexec(h, ['ping', '-c2', '-W1', common.SRV_IP]).returncode != 0]
    if dead:
        sys.exit('srv 로 ping 이 안 되는 호스트: %s\n'
                 '  컨트롤러가 떠 있고 스위치 4대가 붙었는지, python3 apply.py 를 돌렸는지 확인' % ' '.join(dead))


def stop_servers():
    common.dexec('srv', ['sh', '-c', 'killall iperf3 2>/dev/null'])


def fmt(rates):
    return '합 %.1f Mbps' % sum(rates.values())


def run_step(rates, dur, log):
    """호스트마다 rates[h] Mbps 로 dur 초 동시에 보낸다. (lost, total, per_host) 를 돌려준다."""
    procs = []
    for i, h in enumerate(common.HOSTS):
        cmd = ['iperf3', '-c', common.SRV_IP, '-u', '-b', '%.3fM' % rates[h], '-t', str(dur),
               '-p', str(BASE_PORT + i), '-l', '1400', '-J']
        procs.append((h, common.dpopen(h, cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
    per_host, lost, total = {}, 0, 0
    for h, p in procs:
        out, _ = p.communicate(timeout=dur + 30)
        try:
            s = json.loads(out)['end']['sum']
            r = (int(s['lost_packets']), int(s['packets']))
        except (ValueError, KeyError, TypeError):
            log('  %s: 손실 보고 없음: %s' % (h, out.strip()[:120]))
            per_host[h] = None
            continue
        per_host[h] = {'lost': r[0], 'total': r[1]}
        lost += r[0]
        total += r[1]
    return lost, total, per_host


def run_phase(name, steps, log):
    """steps = [(rates, dur), ...] 를 차례로 실행하고 하나의 단계로 합산한다."""
    t0, lost, total, per_host = time.time(), 0, 0, {}
    for rates, dur in steps:
        l, t, ph = run_step(rates, dur, log)
        lost += l
        total += t
        for h, v in ph.items():
            if v:
                acc = per_host.setdefault(h, {'lost': 0, 'total': 0})
                acc['lost'] += v['lost']
                acc['total'] += v['total']
    for v in per_host.values():
        v['loss_pct'] = round(v['lost'] / v['total'] * 100, 2) if v['total'] else 0.0
    t1 = time.time()
    loss = round(lost / total * 100, 2) if total else None
    desc = '%s x %ds' % (fmt(steps[0][0]), steps[0][1]) if len(steps) == 1 else '%s -> %s, %d단계 %d초' % (
        fmt(steps[0][0]), fmt(steps[-1][0]), len(steps), sum(d for _, d in steps))
    log('[%s] %s  %.0f초 걸림  lost=%d/%d  loss=%s%%' % (name, desc, t1 - t0, lost, total, loss))
    return {'name': name, 'steps': [{'total_mbps': round(sum(r.values()), 3), 'duration': d} for r, d in steps],
            'start': t0, 'end': t1, 'lost': lost, 'total': total, 'loss_pct': loss, 'per_host': per_host}


def ramp_steps(low, high, ramp_sec, n):
    """호스트마다 low 에서 high[h] 까지 n 단계로 올린다. 마지막 단계가 high 가 된다."""
    if ramp_sec <= 0:
        return []
    return [({h: low + (high[h] - low) * (k + 1) / n for h in high}, ramp_sec // n) for k in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--episode', type=int, default=common.EPISODE)
    ap.add_argument('--warm', type=int, default=30)
    ap.add_argument('--surge', type=int, default=120)
    ap.add_argument('--cool', type=int, default=20)
    ap.add_argument('--ramp', type=int, default=60, help='서서히 올리는 시간(초). 0 이면 계단형')
    ap.add_argument('--ramp-steps', type=int, default=12)
    ap.add_argument('--scenario', default=None, help='A 균등형 / B 쏠림형 / C 우회로 좁음 (기본: 환경변수 SCENARIO, 없으면 A)')
    a = ap.parse_args()

    running = common.running_containers()
    for h in common.HOSTS + ['srv']:
        if common.ct(h) not in running:
            sys.exit('컨테이너 %s 가 없다. python3 gns3_build.py start 후 apply.py' % h)

    def log(msg):
        print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)

    params = common.load_params()
    sc_name = common.scenario_name(a.scenario)
    sc = common.SCENARIOS[sc_name]
    low = {h: common.LOW_MBPS for h in common.HOSTS}
    high = sc['high']
    log('회차 %d 시작  시나리오 %s (%s)  params=%s' % (
        a.episode, sc_name, sc['title'], {k: params[k] for k in ('threshold', 'split_ratio', 'auto')}))
    start_servers()
    phases = []
    try:
        phases.append(run_phase('warm', [(low, a.warm)], log))
        log('>>> 폭주 시작 (트래픽이 오르기 시작)')
        steps = ramp_steps(common.LOW_MBPS, high, a.ramp, a.ramp_steps)
        if steps:
            phases.append(run_phase('ramp', steps, log))
        phases.append(run_phase('surge', [(high, a.surge)], log))
        log('>>> 폭주 종료')
        phases.append(run_phase('cool', [(low, a.cool)], log))
    finally:
        stop_servers()
    # 회차 손실률 = 트래픽이 오르기 시작한 순간부터 최고치가 끝날 때까지 (ramp + surge)
    hot = [p for p in phases if p['name'] in ('ramp', 'surge')]
    lost, total = sum(p['lost'] for p in hot), sum(p['total'] for p in hot)
    result = {'episode': a.episode, 'tool': 'iperf3', 'scenario': sc_name,
              'params': {k: params[k] for k in ('threshold', 'split_ratio', 'auto')},
              'profile': {'warm': a.warm, 'ramp': a.ramp, 'ramp_steps': a.ramp_steps, 'surge': a.surge,
                          'cool': a.cool, 'low': common.LOW_MBPS, 'high': high,
                          'detour_capacity': sc['detour_capacity']},
              'surge_start': hot[0]['start'], 'surge_end': hot[-1]['end'],
              'loss_pct': round(lost / total * 100, 2) if total else None, 'lost': lost, 'total': total,
              'peak_loss_pct': phases[-2]['loss_pct'],
              'phases': phases}
    path = common.result_json(a.episode)
    common.save_json(path, result)
    log('결과 저장: %s  (폭주 구간 손실률 %s%%)' % (path, result['loss_pct']))


if __name__ == '__main__':
    main()
