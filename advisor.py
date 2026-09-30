#!/usr/bin/env python3
"""회차가 끝난 뒤 실행. 기록을 읽고 LLM 에게 threshold / split_ratio 새 값을 제안받아 params.yaml 에 반영한다.

  python3 advisor.py --episode N [--mock] [--dry-run]

순서 (기획서 3.1, 3.4)
 1. 이번 회차 요약: 기본 경로 최대 사용률, 드롭 합계, 우회 개방 시점(폭주 시작 후 +N초), 손실률
 2. 되돌리기: 직전 회차보다 손실률이 나쁘면 LLM 을 부르지 않고 직전 회차 파라미터로 되돌린다
 3. 아니면 LLM 호출 -> JSON {diagnosis, new_params, rationale}
 4. 허용 범위 밖이면 폐기하고 이전 값 유지
 5. params.yaml 갱신, RUN_DIR/history.json 에 회차 기록 추가, 프롬프트·응답은 RUN_DIR/llm/ 에 저장

--mock        : API 없이 규칙 기반 제안 (루프 시험용, LLM 아님).
--claude-code : Claude Code 명령줄(`claude -p`)로 LLM 호출. Pro 구독으로 동작, API 키 불필요.
(옵션 없음)   : Anthropic API 직접 호출. anthropic 패키지와 ANTHROPIC_API_KEY 필요.
모델은 ADVISOR_MODEL 환경변수 (기본 claude-opus-5).
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

MODEL = os.environ.get('ADVISOR_MODEL', 'claude-opus-5')       # --api 일 때
CC_MODEL = os.environ.get('ADVISOR_CC_MODEL', 'opus')           # --claude-code 일 때 (Claude Code 모델 별칭)
ROLLBACK_TOLERANCE = 0.2   # 손실률 %p. 이보다 더 나빠져야 되돌린다 (측정 잡음 무시)

SCHEMA = {
    'type': 'object',
    'properties': {
        'diagnosis': {'type': 'string'},
        'new_params': {
            'type': 'object',
            'properties': {'threshold': {'type': 'integer'}, 'split_ratio': {'type': 'integer'}},
            'required': ['threshold', 'split_ratio'],
            'additionalProperties': False,
        },
        'rationale': {'type': 'string'},
    },
    'required': ['diagnosis', 'new_params', 'rationale'],
    'additionalProperties': False,
}


# ------------------------------------------------------------------ 요약
def summarize(ep, run_dir):
    rows = common.read_csv(common.stats_csv(ep, run_dir))
    events = common.read_csv(common.events_csv(ep, run_dir))
    result = common.load_json(common.result_json(ep, run_dir), {}) or {}
    s = {'episode': ep, 'samples': len(rows)}
    s['max_default_util'] = max((r['default_util_pct'] for r in rows), default=0.0)
    s['max_detour_util'] = max((r['detour_util_pct'] for r in rows), default=0.0)
    s['max_default_mbps'] = max((r['default_mbps'] for r in rows), default=0.0)
    s['max_detour_mbps'] = max((r['detour_mbps'] for r in rows), default=0.0)
    s['drops'] = int(sum(r['drops'] for r in rows))
    s['drops_default'] = int(sum(r.get('default_drops', 0) or 0 for r in rows))   # 기본 경로에서 버린 것
    s['drops_detour'] = int(sum(r.get('detour_drops', 0) or 0 for r in rows))     # 우회 경로에서 버린 것
    s['drops_before_open'] = int(sum(r['drops'] for r in rows if not r['detour_open']))
    s['max_loss_csv'] = max((r['loss_pct'] for r in rows), default=0.0)
    s['loss_pct'] = result.get('loss_pct')
    if s['loss_pct'] is None:            # iperf 보고가 없으면 큐 드롭 기준으로 대체
        surge_rows = [r for r in rows if result.get('surge_start', 0) <= r['ts'] <= result.get('surge_end', 1e12)] or rows
        sent_est = sum(r['drops'] for r in surge_rows)
        s['loss_pct'] = round(sum(r['loss_pct'] for r in surge_rows) / len(surge_rows), 2) if surge_rows else 0.0
        s['loss_source'] = 'csv'
    else:
        s['loss_source'] = 'iperf'
    opens = [e for e in events if e['event'] == 'detour_open']
    s['detour_opened'] = bool(opens)
    # CSV 는 10초 평균이라 순간 최대치가 묻힌다. 컨트롤러가 1초 단위로 본 개방 시점 사용률도 반영
    for e in opens:
        m = re.search(r'util=([\d.]+)', str(e.get('detail', '')))
        if m:
            s['max_default_util'] = max(s['max_default_util'], float(m.group(1)))
    s['detour_open_after_s'] = None
    if opens and result.get('surge_start'):
        s['detour_open_after_s'] = round(opens[0]['ts'] - result['surge_start'], 1)
    s['scenario'] = result.get('scenario')          # 그래프용. 프롬프트에는 넣지 않는다
    s['peak_loss_pct'] = result.get('peak_loss_pct')
    s['host_mbps'] = host_peak_mbps(ep, run_dir)
    s['detour_capacity'] = (result.get('profile') or {}).get('detour_capacity', common.CAPACITY_MBPS)
    s['params'] = result.get('params') or {k: common.load_params()[k] for k in ('threshold', 'split_ratio', 'auto')}
    return s


def fmt_prev(history):
    if not history:
        return '없음 (첫 회차)'
    lines = []
    for h in history[-4:]:
        p = h['params']
        lines.append('ep%d: threshold=%s split_ratio=%s -> 손실률 %s%%, 최대 사용률 %.0f%%, 우회 개방 %s, 조치=%s' % (
            h['episode'], p['threshold'], p['split_ratio'], h['loss_pct'], h['max_default_util'],
            ('+%ss' % h['detour_open_after_s']) if h.get('detour_open_after_s') is not None else '안 열림',
            h.get('action', '')))
    return '\n'.join(lines)


def host_peak_mbps(ep, run_dir):
    """_hosts.csv 에서 최고치 구간(전체 송신량이 최대의 80% 이상인 구간)의 호스트별 평균 Mbps."""
    rows = common.read_csv(common.stats_csv(ep, run_dir).replace('.csv', '_hosts.csv'))
    if not rows:
        return {}
    tot = [sum(r[h] for h in common.HOSTS) for r in rows]
    peak = [r for r, t in zip(rows, tot) if t >= max(tot) * 0.8]
    return {h: round(sum(r[h] for r in peak) / len(peak), 2) for h in common.HOSTS}


def fmt_hosts(hm):
    if not hm:
        return '기록 없음'
    return ', '.join('%s %.1f' % (h, hm[h]) for h in common.HOSTS) + ' (합 %.1f Mbps)' % sum(hm.values())


def build_prompt(s, params, history):
    lim = params['limits']
    opened = ('폭주 후 +%s초' % s['detour_open_after_s']) if s['detour_open_after_s'] is not None \
        else ('열림(시점 미상)' if s['detour_opened'] else '열리지 않음')
    return (
        '[이번 회차에 쓴 파라미터] threshold=%s, split_ratio=%s\n'
        '[이번 회차 ep%d] 기본 경로 최대 %.1f Mbps (사용률 %.0f%%), 우회 경로 최대 %.1f Mbps (사용률 %.0f%%), '
        '드롭 %d pkt (기본 경로 %d, 우회 경로 %d, 우회 개방 전 %d), 우회로 개방 시점 %s, 손실률 %s%%\n'
        '[호스트별 송신량, 최고치 구간 평균 Mbps] %s\n'
        '[링크 가용 대역] 기본 경로 %.0f Mbps, 우회 경로 %.0f Mbps\n'
        '[이전 회차]\n%s\n'
        '[허용 범위] threshold %d–%d, split_ratio %d–%d\n\n'
        '다음 폭주에 대비해 손실률을 낮추기 위한 새 파라미터를 JSON 으로 제안하라.'
        % (s['params']['threshold'], s['params']['split_ratio'], s['episode'],
           s['max_default_mbps'], s['max_default_util'], s['max_detour_mbps'], s['max_detour_util'], s['drops'], s['drops_default'], s['drops_detour'],
           s['drops_before_open'], opened, s['loss_pct'], fmt_hosts(s['host_mbps']),
           common.CAPACITY_MBPS, s['detour_capacity'], fmt_prev(history),
           lim['threshold'][0], lim['threshold'][1], lim['split_ratio'][0], lim['split_ratio'][1]))


# ------------------------------------------------------------------ 제안
def ask_llm(system, prompt):
    import anthropic  # 여기서 임포트해야 --mock 이 SDK 없이도 돈다
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=2000, system=system,
        messages=[{'role': 'user', 'content': prompt}],
        output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}},
    )
    if resp.stop_reason not in ('end_turn', 'stop_sequence'):
        raise RuntimeError('LLM 응답 중단: stop_reason=%s' % resp.stop_reason)
    text = next(b.text for b in resp.content if b.type == 'text')
    return json.loads(text), {'model': resp.model, 'usage': resp.usage.model_dump()}


def ask_claude_code(system, prompt):
    """Pro 요금제에 포함된 Claude Code 명령줄(`claude -p`)로 묻는다. API 키·추가 요금 불필요 (Pro 사용량에서 차감).
    도구는 전부 끄고(--tools ""), 출력은 JSON 스키마로 강제한다. 프로젝트 CLAUDE.md 가 섞이지 않게 /tmp 에서 실행."""
    import subprocess
    cmd = ['claude', '-p', '--model', CC_MODEL, '--system-prompt', system, '--tools', '',
           '--json-schema', json.dumps(SCHEMA), '--output-format', 'json', '--no-session-persistence']
    last = None
    for attempt in (1, 2):                      # 한 번 실패하면 한 번만 다시
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600, cwd='/tmp')
        try:
            d = json.loads(r.stdout)
            out = d.get('structured_output') or json.loads(d['result'])
            if not d.get('is_error') and out and 'new_params' in out:
                return out, {'model': 'claude-code:' + ','.join(d.get('modelUsage', {}).keys()),
                             'duration_ms': d.get('duration_ms'), 'attempt': attempt,
                             'note': 'Pro 구독 사용량 (total_cost_usd 는 API 환산 추정치, 청구 아님)',
                             'cost_equiv_usd': d.get('total_cost_usd')}
            last = 'is_error=%s result=%s' % (d.get('is_error'), str(d.get('result'))[:200])
        except (ValueError, KeyError, TypeError) as e:
            last = '%r stdout=%s stderr=%s' % (e, r.stdout[:200], r.stderr[:200])
    raise RuntimeError('claude -p 실패: %s' % last)


def mock_llm(s, params):
    """API 없이 knowledge.md 의 규칙을 흉내 낸 제안."""
    th, sr = int(params['threshold']), int(params['split_ratio'])
    why = []
    late = s['detour_open_after_s']
    if s['drops'] == 0 and (s['loss_pct'] or 0) == 0:
        return {'diagnosis': '(mock) 드롭 0, 손실 0 - 고칠 것 없음',
                'new_params': {'threshold': th, 'split_ratio': sr},
                'rationale': '손실이 없으면 유지 (knowledge.md)'}, {'model': 'mock'}
    step = 10     # 호스트 10대라 split_ratio 는 10% 가 한 대
    if not s['detour_opened'] or s['drops_before_open'] > 0:
        th -= 10
        why.append('우회로가 열리기 전에 드롭이 %d개 났으니 threshold 를 10 낮춤' % s['drops_before_open'])
    if s['drops_default'] > s['drops_detour'] and s['detour_opened']:
        sr += step
        why.append('기본 경로에서 더 많이 버려졌으니(%d > %d) split_ratio 를 %d 올림' % (
            s['drops_default'], s['drops_detour'], step))
    elif s['drops_detour'] > s['drops_default']:
        sr -= step
        why.append('우회 경로에서 더 많이 버려졌으니(%d > %d) split_ratio 를 %d 낮춤' % (
            s['drops_detour'], s['drops_default'], step))
    if not why:
        why.append('이대로 유지')
    return {'diagnosis': '(mock) 최대 사용률 %.0f%%, 드롭 %d, 손실률 %s%%' % (
                s['max_default_util'], s['drops'], s['loss_pct']),
            'new_params': {'threshold': th, 'split_ratio': sr},
            'rationale': '; '.join(why)}, {'model': 'mock'}


def in_range(new, lim):
    return (lim['threshold'][0] <= new['threshold'] <= lim['threshold'][1]
            and lim['split_ratio'][0] <= new['split_ratio'] <= lim['split_ratio'][1])


# ------------------------------------------------------------------ 메인
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--episode', type=int, default=common.EPISODE)
    ap.add_argument('--run-dir', default=common.RUN_DIR)
    ap.add_argument('--mock', action='store_true', help='LLM 대신 규칙 기반 제안')
    ap.add_argument('--claude-code', action='store_true', help='API 대신 Claude Code 명령줄(Pro 구독)로 LLM 호출')
    ap.add_argument('--dry-run', action='store_true', help='params.yaml / history 를 건드리지 않는다')
    a = ap.parse_args()

    params = common.load_params()
    history = common.load_json(common.history_json(a.run_dir), []) or []
    history = [h for h in history if h['episode'] != a.episode]     # 같은 회차 재실행 대비
    s = summarize(a.episode, a.run_dir)
    if s['samples'] == 0:
        sys.exit('통계가 없다: %s' % common.stats_csv(a.episode, a.run_dir))
    print('[요약] ep%d params=%s 최대사용률=%.0f%% 드롭=%d 손실률=%s%%(%s) 우회개방=%s' % (
        a.episode, s['params'], s['max_default_util'], s['drops'], s['loss_pct'],
        s['loss_source'], s['detour_open_after_s']))

    prev = history[-1] if history else None
    record = dict(s, action='', llm=None, applied=None)
    new_params = dict(params)

    # 2. 되돌리기 (기획서 3.4): 직전 회차에 값을 바꿨는데 손실이 나빠졌으면 직전 값으로.
    #    값을 안 바꿨는데 나빠졌다면 폭주 자체가 바뀐 것이므로 되돌리지 않고 조언자에게 맡긴다 (CHANGES.md 25번)
    changed = prev is not None and any(
        s['params'][k] != prev['params'][k] for k in ('threshold', 'split_ratio'))
    if prev and changed and prev['loss_pct'] is not None and s['loss_pct'] is not None \
            and s['loss_pct'] > prev['loss_pct'] + ROLLBACK_TOLERANCE:
        new_params.update({k: prev['params'][k] for k in ('threshold', 'split_ratio')})
        record['action'] = 'rollback'
        print('[되돌림] 손실률 %s%% > 직전 %s%%  ->  ep%d 값 threshold=%s split_ratio=%s 으로 복귀' % (
            s['loss_pct'], prev['loss_pct'], prev['episode'], new_params['threshold'], new_params['split_ratio']))
    else:
        # 3. LLM
        with open(os.path.join(common.BASE_DIR, 'knowledge.md')) as f:
            system = ('너는 SDN 네트워크 운영자다. 아래 요령을 참고해 다음 회차의 파라미터를 제안한다. '
                      '반드시 JSON 만 출력한다.\n\n' + f.read())
        prompt = build_prompt(s, params, history)
        llm_dir = os.path.join(a.run_dir, 'llm')
        os.makedirs(llm_dir, exist_ok=True)
        with open(os.path.join(llm_dir, 'ep_%d_prompt.txt' % a.episode), 'w') as f:
            f.write('### system\n' + system + '\n\n### user\n' + prompt)
        try:
            if a.mock:
                out, meta = mock_llm(s, params)
            elif a.claude_code:
                out, meta = ask_claude_code(system, prompt)
            else:
                out, meta = ask_llm(system, prompt)
        except Exception as e:      # API 실패 시 값 유지
            out, meta = None, {'error': repr(e)}
            print('[LLM 실패] %r -> 파라미터 유지' % e)
        common.save_json(os.path.join(llm_dir, 'ep_%d_response.json' % a.episode),
                         {'meta': meta, 'response': out, 'ts': time.time()})
        record['llm'] = out
        if out:
            print('[진단] %s\n[제안] %s\n[근거] %s' % (out['diagnosis'], out['new_params'], out['rationale']))
            # 4. 범위 검사
            if in_range(out['new_params'], params['limits']):
                new_params.update(out['new_params'])
                record['action'] = 'apply'
            else:
                record['action'] = 'rejected_out_of_range'
                print('[폐기] 허용 범위 밖 -> 이전 값 유지')
        else:
            record['action'] = 'llm_error'

    record['applied'] = {k: new_params[k] for k in ('threshold', 'split_ratio')}
    history.append(record)
    if a.dry_run:
        print('[dry-run] 다음 회차 파라미터 (미반영): %s' % record['applied'])
        return
    common.save_params(new_params)
    common.save_json(common.history_json(a.run_dir), history)
    print('[반영] params.yaml -> %s' % record['applied'])


if __name__ == '__main__':
    main()
