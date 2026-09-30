#!/usr/bin/env python3
"""그래프.

  python3 plot.py timeline RUN_DIR EP          -> RUN_DIR/plots/ep_EP_timeline.png (한 회차 시간축)
  python3 plot.py summary RUN_DIR [RUN_DIR2..] -> 첫 RUN_DIR/plots/summary_loss.png (회차별 손실률, 여러 run 비교)
  python3 plot.py compare OUT.png RUN_DIR ...   -> 시나리오 구간을 표시한 비교 그래프 (고정 기준선은 가로 점선)
"""
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# 팔레트: 계열 1(파랑)=기본 경로, 2(주황)=우회 경로, 8(빨강)=드롭
C_DEFAULT, C_DETOUR, C_DROP = '#2a78d6', '#eb6834', '#e34948'
INK, MUTED, GRID, SURFACE = '#0b0b0b', '#898781', '#e1e0d9', '#fcfcfb'
plt.rcParams.update({'font.family': ['Noto Sans CJK KR', 'Noto Sans CJK JP', 'NanumGothic', 'DejaVu Sans'],
                     'axes.unicode_minus': False, 'axes.edgecolor': GRID,
                     'axes.labelcolor': MUTED, 'xtick.color': MUTED, 'ytick.color': MUTED,
                     'text.color': INK, 'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE})


def _style(ax):
    ax.set_axisbelow(True)
    ax.grid(True, axis='y', color=GRID, linewidth=0.8)
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)


def timeline(run_dir, ep):
    rows = common.read_csv(common.stats_csv(ep, run_dir))
    if not rows:
        sys.exit('통계가 없다: %s' % common.stats_csv(ep, run_dir))
    events = common.read_csv(common.events_csv(ep, run_dir))
    result = common.load_json(common.result_json(ep, run_dir), {}) or {}
    t0 = rows[0]['ts'] - rows[0]['elapsed_s']
    x = [r['elapsed_s'] for r in rows]

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True,
                                 gridspec_kw={'height_ratios': [3, 2]})
    a1.plot(x, [r['default_util_pct'] for r in rows], color=C_DEFAULT, lw=2, marker='o', ms=4,
            label='기본 경로 (s1→s2)')
    a1.plot(x, [r['detour_util_pct'] for r in rows], color=C_DETOUR, lw=2, marker='o', ms=4,
            label='우회 경로 (s1→s3)')
    a1.axhline(100, color=MUTED, lw=0.8, ls=':')
    a1.set_ylabel('링크 사용률 (%)')
    a1.set_ylim(0, max(110, max(r['default_util_pct'] for r in rows) + 10))
    # 폭주 구간 음영 + 우회 개방/해제 선
    if result.get('surge_start'):
        a1.axvspan(result['surge_start'] - t0, result['surge_end'] - t0, color=GRID, alpha=0.5,
                   label='폭주 구간')
        a2.axvspan(result['surge_start'] - t0, result['surge_end'] - t0, color=GRID, alpha=0.5)
    for e in events:
        if e['event'] in ('detour_open', 'detour_close'):
            col = C_DETOUR if e['event'] == 'detour_open' else MUTED
            for ax in (a1, a2):
                ax.axvline(e['ts'] - t0, color=col, ls='--', lw=1.2)
            a1.text(e['ts'] - t0, a1.get_ylim()[1] * 0.97, '우회 개방' if e['event'] == 'detour_open' else '우회 해제',
                    color=col, fontsize=9, ha='left', va='top')
    a1.legend(loc='upper left', frameon=False, fontsize=9)
    _style(a1)

    width = (x[1] - x[0]) * 0.8 if len(x) > 1 else 8
    a2.bar(x, [r['drops'] for r in rows], width=width, color=C_DROP, label='드롭 (pkt / 구간)')
    a2.set_ylabel('드롭 (pkt)')
    a2.set_xlabel('경과 시간 (초)')
    _style(a2)

    p = result.get('params', {})
    fig.suptitle('회차 %d — threshold=%s split_ratio=%s auto=%s, 폭주 구간 손실률 %s%%' % (
        ep, p.get('threshold', '?'), p.get('split_ratio', '?'), p.get('auto', '?'),
        result.get('loss_pct', '?')), fontsize=11)
    out = os.path.join(run_dir, 'plots', 'ep_%d_timeline.png' % ep)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(out)


def summary(run_dirs):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = [C_DEFAULT, C_DETOUR, '#1baf7a', '#eda100']
    for i, rd in enumerate(run_dirs):
        hist = common.load_json(common.history_json(rd), []) or []
        if not hist:
            print('history 없음: %s' % rd)
            continue
        eps = [h['episode'] for h in hist]
        loss = [h['loss_pct'] or 0 for h in hist]
        ax.plot(eps, loss, color=colors[i % len(colors)], lw=2, marker='o', ms=6,
                label=os.path.basename(os.path.normpath(rd)))
        for h in hist:
            ax.annotate('th%s/sr%s' % (h['params']['threshold'], h['params']['split_ratio']),
                        (h['episode'], h['loss_pct'] or 0), textcoords='offset points',
                        xytext=(0, 8), ha='center', fontsize=8, color=MUTED)
    ax.set_xlabel('회차')
    ax.set_ylabel('폭주 구간 손실률 (%)')
    ax.set_ylim(bottom=0)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.legend(frameon=False)
    ax.set_title('회차별 손실률')
    _style(ax)
    out = os.path.join(run_dirs[0], 'plots', 'summary_loss.png')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(out)


def compare(out, run_dirs):
    """여러 run 의 회차별 손실률을 시나리오 구간과 함께 한 그래프에.
    회차가 시나리오마다 1개씩뿐인 run(고정값 기준선)은 그 시나리오 구간 전체에 가로선으로 그린다."""
    hists = {}
    for rd in run_dirs:
        h = common.load_json(common.history_json(rd), []) or []
        if h:
            hists[rd] = sorted(h, key=lambda x: x['episode'])
    longest = max(hists.values(), key=len)
    seq = [(x['episode'], x.get('scenario') or 'A') for x in longest]
    fig, ax = plt.subplots(figsize=(11, 5))
    # 시나리오 구간 배경
    spans, cur = [], None
    for ep, sc in seq:
        if cur and cur[0] == sc:
            cur[2] = ep
        else:
            cur = [sc, ep, ep]
            spans.append(cur)
    shade = ['#f0efec', '#fcfcfb']
    for i, (sc, a, b) in enumerate(spans):
        ax.axvspan(a - 0.5, b + 0.5, color=shade[i % 2], zorder=0)
        ax.text((a + b) / 2, 1.0, '%s  %s' % (sc, common.SCENARIOS[sc]['title']),
                transform=ax.get_xaxis_transform(), ha='center', va='bottom', fontsize=9, color=MUTED)
    colors = [MUTED, C_DETOUR, C_DEFAULT, '#1baf7a']
    names = {'sc_fixed': '고정 90/20 (안 고침)', 'sc_rule': '규칙 (mock)', 'sc_rule_v1': '규칙 (mock, 되돌리기 수정 전)',
             'sc_llm': 'LLM'}
    for i, (rd, h) in enumerate(hists.items()):
        base = os.path.basename(os.path.normpath(rd))
        label = names.get(base, base)
        col = colors[i % len(colors)]
        scs = [x.get('scenario') or 'A' for x in h]
        if len(h) == len(set(scs)) and len(h) < len(longest):          # 시나리오마다 1회 = 고정 기준선
            for x in h:
                for sc, a, b in spans:
                    if sc == x.get('scenario'):
                        ax.hlines(x['loss_pct'], a - 0.4, b + 0.4, colors=col, linestyles='--', lw=2,
                                  label=label if sc == scs[0] else None)
            continue
        eps = [x['episode'] for x in h]
        ax.plot(eps, [x['loss_pct'] or 0 for x in h], color=col, lw=2, marker='o', ms=6, label=label, zorder=3)
        for x in h:
            mark = {'rollback': ' ↺', 'rejected_out_of_range': ' ✕'}.get(x.get('action'), '')
            ax.annotate('%s/%s%s' % (x['params']['threshold'], x['params']['split_ratio'], mark),
                        (x['episode'], x['loss_pct'] or 0), textcoords='offset points', xytext=(0, 8),
                        ha='center', fontsize=7.5, color=col)
    ax.set_xlabel('회차')
    ax.set_ylabel('폭주 구간 손실률 (%)')
    ax.set_ylim(bottom=0)
    ax.set_xticks([e for e, _ in seq])
    ax.legend(frameon=False, loc='upper center', bbox_to_anchor=(0.5, -0.13), ncol=4)
    _style(ax)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    fig.savefig(out, dpi=130)
    print(out)


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == 'timeline':
        timeline(sys.argv[2], int(sys.argv[3]))
    elif len(sys.argv) >= 4 and sys.argv[1] == 'compare':
        compare(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) >= 3 and sys.argv[1] == 'summary':
        summary(sys.argv[2:])
    else:
        sys.exit(__doc__)
