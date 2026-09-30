"""통계 수집기 (Ryu 앱). controller.py 와 같은 ryu-manager 에 올린다.

STATS_INTERVAL(기본 10초)마다 RUN_DIR/stats/ep_N.csv 에 한 줄:
  ts, elapsed_s, default_mbps, default_util_pct, detour_mbps, detour_util_pct, drops, loss_pct, detour_open,
  default_drops, detour_drops

- 대역폭: s1 의 포트 통계(tx_bytes) 차이. meter 를 통과해 실제로 나간 양이므로 최대 10 Mbps
- drops: s1 의 meter 통계(band 의 packet_band_count) 차이 = 용량을 넘어 버린 패킷 (기본+우회 합)
- loss_pct: drops / (보낸 패킷 + drops) * 100
- detour_open: controller.py 가 쓰는 ctrl_state.json

s1 의 포트 통계 응답은 컨트롤러가 1초마다 받는 것도 이 앱에 같이 들어오므로, 여기서는 최근 누적값만 기억해 두고
STATS_INTERVAL 마다 이전 기록과의 차이를 낸다. meter 통계는 이 앱이 직접 요청한다.
"""
import csv
import os
import sys
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import topo    # noqa: E402


class StatsMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dp1 = None
        self.port = {}                 # ifname -> ofport (s1)
        self.tx = {}                   # ofport -> (bytes, pkts)
        self.drop = {}                 # meter_id -> dropped pkts
        self.prev = None
        self.host_bytes = {}          # 호스트 -> 누적 바이트 (규칙이 바뀌어도 이어지게 보정)
        self.host_bytes_base = {}
        self._last_flow_bytes = {}
        self.host_prev = None
        self.t0 = time.time()
        self.host_path = common.stats_csv().replace('.csv', '_hosts.csv')
        with open(self.host_path, 'w', newline='') as f:
            csv.writer(f).writerow(['ts', 'elapsed_s'] + common.HOSTS)
        self.path = common.stats_csv()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', newline='') as f:
            csv.writer(f).writerow(common.STATS_COLUMNS)
        self.logger.info('[monitor] %s 에 %.0f초마다 기록', self.path, common.STATS_INTERVAL)
        self.thread = hub.spawn(self._loop)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc(self, ev):
        dp = ev.msg.datapath
        if dp.id == 1:
            self.dp1 = dp
            self.port = {p.name.decode().strip('\x00'): p.port_no for p in ev.msg.body
                         if p.port_no < dp.ofproto.OFPP_MAX}

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        if ev.msg.datapath.id == 1:
            self.tx = {s.port_no: (s.tx_bytes, s.tx_packets) for s in ev.msg.body}

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats(self, ev):
        """S1 의 호스트별 출구 규칙(priority 20) 카운터 = 그 호스트가 보낸 양 (meter 로 버려지기 전)."""
        if ev.msg.datapath.id != 1:
            return
        port2host = {self.port.get(ifn): h for h, ifn in zip(common.HOSTS, topo.S1_HOST_IFS)}
        for f in ev.msg.body:
            if f.priority == 20 and 'in_port' in f.match:
                h = port2host.get(f.match['in_port'])
                if h:
                    last = self._last_flow_bytes.get(h, 0)
                    if f.byte_count < last:          # 규칙이 덮어써져 카운터가 줄었다 -> 이전 값을 기반에 더함
                        self.host_bytes_base[h] = self.host_bytes_base.get(h, 0) + last
                    self.host_bytes[h] = self.host_bytes_base.get(h, 0) + f.byte_count
                    self._last_flow_bytes[h] = f.byte_count

    @set_ev_cls(ofp_event.EventOFPMeterStatsReply, MAIN_DISPATCHER)
    def _meter_stats(self, ev):
        if ev.msg.datapath.id == 1:
            self.drop = {m.meter_id: sum(b.packet_band_count for b in m.band_stats) for m in ev.msg.body}

    def _loop(self):
        hub.sleep(common.STATS_INTERVAL)
        while True:
            if self.dp1 is not None:
                p, ofp = self.dp1.ofproto_parser, self.dp1.ofproto
                self.dp1.send_msg(p.OFPPortStatsRequest(self.dp1, 0, ofp.OFPP_ANY))
                self.dp1.send_msg(p.OFPMeterStatsRequest(self.dp1, 0, ofp.OFPM_ALL))
                self.dp1.send_msg(p.OFPFlowStatsRequest(self.dp1, table_id=ofp.OFPTT_ALL))
                hub.sleep(0.5)
                self._record()
                hub.sleep(common.STATS_INTERVAL - 0.5)
            else:
                hub.sleep(common.STATS_INTERVAL)

    def _snapshot(self):
        d, v = self.port.get(topo.S1_DEFAULT_IF), self.port.get(topo.S1_DETOUR_IF)
        if d is None or v is None:
            return None
        tb, tp = self.tx.get(d, (0, 0))
        vb, vp = self.tx.get(v, (0, 0))
        return (time.time(), tb, tp, self.drop.get(d, 0), vb, vp, self.drop.get(v, 0))

    def _record(self):
        cur = self._snapshot()
        if cur is None:
            return
        if self.prev is None:
            self.prev = cur
            return
        dt = cur[0] - self.prev[0]
        if dt <= 0:
            return
        d_bytes, d_pkts, d_drop = (cur[i] - self.prev[i] for i in (1, 2, 3))
        v_bytes, v_pkts, v_drop = (cur[i] - self.prev[i] for i in (4, 5, 6))
        d_mbps, v_mbps = d_bytes * 8 / dt / 1e6, v_bytes * 8 / dt / 1e6
        drops, sent = d_drop + v_drop, d_pkts + v_pkts
        loss = drops / (sent + drops) * 100 if (sent + drops) > 0 else 0.0
        state = common.load_json(common.state_json(), {}) or {}
        line = ['%.3f' % cur[0], '%.1f' % (cur[0] - self.t0),
                '%.3f' % d_mbps, '%.1f' % (d_mbps / common.CAPACITY_MBPS * 100),
                '%.3f' % v_mbps, '%.1f' % (v_mbps / common.detour_capacity() * 100),
                '%d' % drops, '%.2f' % loss, '1' if state.get('detour_open') else '0',
                '%d' % d_drop, '%d' % v_drop]
        with open(self.path, 'a', newline='') as f:
            csv.writer(f).writerow(line)
        self.logger.info('[stats] 기본 %.1f%% 우회 %.1f%% drops=%d(기본 %d, 우회 %d) loss=%.2f%% detour=%s',
                         d_mbps / common.CAPACITY_MBPS * 100, v_mbps / common.detour_capacity() * 100,
                         drops, d_drop, v_drop, loss, line[8])
        self.prev = cur
        self._record_hosts(cur[0])

    def _record_hosts(self, now):
        """호스트별 Mbps 를 _hosts.csv 에. 우회를 열면 규칙이 덮어써져 카운터가 0 부터 다시 세므로 보정한다."""
        cur = dict(self.host_bytes)
        if self.host_prev is not None and cur:
            dt = now - self.host_prev[0]
            row = ['%.3f' % now, '%.1f' % (now - self.t0)]
            for h in common.HOSTS:
                d = cur.get(h, 0) - self.host_prev[1].get(h, 0)
                row.append('%.3f' % max(0.0, d * 8 / dt / 1e6))
            with open(self.host_path, 'a', newline='') as f:
                csv.writer(f).writerow(row)
        self.host_prev = (now, cur)
