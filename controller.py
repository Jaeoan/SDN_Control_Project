"""폭주 대응 컨트롤러 (Ryu, OpenFlow 1.3) — GNS3 판.

- 스위치가 붙으면 port-desc 로 인터페이스 이름→OpenFlow 포트 번호를 알아낸 뒤 고정 경로 규칙을 깐다.
- 스위치 간 나가는 포트마다 10 Mbps meter 를 건다 (이 환경은 userspace datapath 라 tc 가 아닌 meter 로 용량을 만든다).
- 1초마다 s1 포트 통계로 기본 경로 사용률을 구한다. auto 이고 사용률 >= threshold 면 split_ratio 만큼 호스트를 우회로로.
  split_ratio 는 호스트 수로 환산: int(5*ratio/100+0.5) 대가 h1 부터 우회.
- 두 경로 합계 부하가 threshold 의 절반 밑으로 20초 이어지면 우회를 닫는다.
- RUN_DIR/detour_cmd 에 "open 50" / "close" 를 쓰면 수동 개방/해제 (manual_detour.sh).
- 열고 닫을 때 RUN_DIR/stats/ep_N_events.csv 에 기록, RUN_DIR/ctrl_state.json 을 1초마다 갱신.

실행: RUN_DIR=runs/test EPISODE=1 ryu-manager --ofp-tcp-listen-port 6655 controller.py monitor.py
"""
import json
import os
import sys
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import topo    # noqa: E402

POLL_SEC = 1.0
CLOSE_AFTER_SEC = 20
DPID2SW = {dpid: name for name, (dpid, ip) in topo.SWITCHES.items()}


class SurgeController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.params = common.load_params()
        self.dps = {}
        self.port = {}           # dpid -> {ifname: ofport}
        self.detour_open = False
        self.detour_ratio = 0
        self.util = 0.0
        self.total_mbps = 0.0
        self.last = None
        self.low_since = None
        os.makedirs(common.RUN_DIR, exist_ok=True)
        common.stats_dir()
        self.logger.info('[params] threshold=%s split_ratio=%s auto=%s  (RUN_DIR=%s EPISODE=%d SCENARIO=%s, 우회 대역 %.0f Mbps)',
                         self.params['threshold'], self.params['split_ratio'],
                         self.params['auto'], common.RUN_DIR, common.EPISODE,
                         common.scenario_name(), common.detour_capacity())
        common.append_event('controller_start', 'threshold=%s split_ratio=%s auto=%s' % (
            self.params['threshold'], self.params['split_ratio'], self.params['auto']))
        self.thread = hub.spawn(self._loop)

    # ------------------------------------------------------------ 접속·규칙
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        if dp.id not in DPID2SW:
            self.logger.warning('[경고] 모르는 스위치 dpid=%d', dp.id)
            return
        self.dps[dp.id] = dp
        dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc(self, ev):
        dp = ev.msg.datapath
        if dp.id not in DPID2SW:
            return
        self.port[dp.id] = {p.name.decode().strip('\x00'): p.port_no for p in ev.msg.body
                            if p.port_no < dp.ofproto.OFPP_MAX}
        sw = DPID2SW[dp.id]
        self._clear(dp)
        self._install_meters(dp, sw)
        getattr(self, '_install_' + sw.lower())(dp)
        self.logger.info('[연결] %s(dpid=%d) 포트 %s 규칙·meter 설치', sw, dp.id, self.port[dp.id])

    def _clear(self, dp):
        ofp, p = dp.ofproto, dp.ofproto_parser
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                 out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))
        dp.send_msg(p.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE, meter_id=ofp.OFPM_ALL))

    def _install_meters(self, dp, sw):
        """스위치 간 나가는 포트마다 meter. meter_id = 그 포트의 ofport."""
        ofp, p = dp.ofproto, dp.ofproto_parser
        for ifname, peer in topo.inter_switch_ifs(sw):
            port = self.port[dp.id].get(ifname)
            if port is None:
                self.logger.warning('[경고] %s 에 %s 없음', sw, ifname)
                continue
            cap = common.CAPACITY_MBPS
            if sw == 'S1' and ifname == topo.S1_DETOUR_IF:      # 시나리오 C: 우회 경로가 좁다
                cap = common.detour_capacity()
            band = p.OFPMeterBandDrop(rate=int(cap * 1000), burst_size=2000)
            dp.send_msg(p.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                      flags=ofp.OFPMF_KBPS | ofp.OFPMF_BURST,
                                      meter_id=port, bands=[band]))

    def _p(self, dp, ifname):
        return self.port[dp.id][ifname]

    def _flow(self, dp, priority, match, out_ifs, meter=True):
        """out_ifs 로 내보낸다. 스위치 간 포트로 나가면 그 포트의 meter 를 거친다."""
        ofp, p = dp.ofproto, dp.ofproto_parser
        sw = DPID2SW[dp.id]
        inter = {ifn for ifn, _ in topo.inter_switch_ifs(sw)}
        acts = [p.OFPActionOutput(self._p(dp, o)) for o in out_ifs]
        inst = []
        if meter and len(out_ifs) == 1 and out_ifs[0] in inter:
            inst.append(p.OFPInstructionMeter(self._p(dp, out_ifs[0])))
        inst.append(p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, acts))
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst))

    def _install_s1(self, dp):
        p = dp.ofproto_parser
        hp, d, v = topo.S1_HOST_IFS, topo.S1_DEFAULT_IF, topo.S1_DETOUR_IF
        # ARP 등 IP 아닌 것: 호스트 -> 기본 경로, 경로 -> 호스트 전부 (우회 경로로는 안 보내므로 루프 없음)
        for ifn in hp:
            self._flow(dp, 5, p.OFPMatch(in_port=self._p(dp, ifn)), [d])
        for ifn in (d, v):
            self._flow(dp, 5, p.OFPMatch(in_port=self._p(dp, ifn)), hp)
        # 돌아오는 IP: 목적지 IP 로 호스트 포트
        for ifn, h in zip(hp, common.HOSTS):
            self._flow(dp, 10, p.OFPMatch(eth_type=0x0800, ipv4_dst=common.HOST_IP[h]), [ifn])
        self._apply_split(0)

    def _install_s2(self, dp):
        self._install_pipe(dp)

    def _install_s3(self, dp):
        self._install_pipe(dp)

    def _install_pipe(self, dp):
        p = dp.ofproto_parser
        self._flow(dp, 10, p.OFPMatch(in_port=self._p(dp, 'eth1')), ['eth2'])
        self._flow(dp, 10, p.OFPMatch(in_port=self._p(dp, 'eth2')), ['eth1'])

    def _install_s4(self, dp):
        p = dp.ofproto_parser
        srv = topo.HOSTS['srv'][2]
        self._flow(dp, 10, p.OFPMatch(in_port=self._p(dp, 'eth1')), [srv])
        self._flow(dp, 10, p.OFPMatch(in_port=self._p(dp, 'eth2')), [srv])
        self._flow(dp, 10, p.OFPMatch(in_port=self._p(dp, srv)), ['eth1'])   # 돌아가는 길은 기본 경로

    def _apply_split(self, ratio):
        dp = self.dps.get(1)
        if dp is None or 1 not in self.port:
            return
        p = dp.ofproto_parser
        n = common.detour_hosts(ratio)
        for i, ifn in enumerate(topo.S1_HOST_IFS):
            out = topo.S1_DETOUR_IF if i < n else topo.S1_DEFAULT_IF
            self._flow(dp, 20, p.OFPMatch(in_port=self._p(dp, ifn), eth_type=0x0800), [out])
        return n

    # ------------------------------------------------------------ 열기/닫기
    def open_detour(self, ratio, reason):
        n = self._apply_split(ratio)
        self.detour_open, self.detour_ratio, self.low_since = True, int(ratio), None
        detail = 'ratio=%d hosts=%d util=%.1f reason=%s' % (ratio, n or 0, self.util, reason)
        self.logger.info('[우회 개방] %s', detail)
        common.append_event('detour_open', detail)

    def close_detour(self, reason):
        self._apply_split(0)
        self.detour_open, self.detour_ratio, self.low_since = False, 0, None
        detail = 'util=%.1f reason=%s' % (self.util, reason)
        self.logger.info('[우회 해제] %s', detail)
        common.append_event('detour_close', detail)

    # ------------------------------------------------------------ 감시 루프
    def _loop(self):
        while True:
            dp = self.dps.get(1)
            if dp is not None:
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            self._check_cmd()
            self._write_state()
            hub.sleep(POLL_SEC)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _stats(self, ev):
        if ev.msg.datapath.id != 1 or 1 not in self.port:
            return
        now = time.time()
        tx = {s.port_no: s.tx_bytes for s in ev.msg.body}
        cur = (now, tx.get(self.port[1][topo.S1_DEFAULT_IF], 0), tx.get(self.port[1][topo.S1_DETOUR_IF], 0))
        if self.last and cur[0] - self.last[0] >= 0.5:
            dt = cur[0] - self.last[0]
            d_mbps = (cur[1] - self.last[1]) * 8 / dt / 1e6
            v_mbps = (cur[2] - self.last[2]) * 8 / dt / 1e6
            self.util = d_mbps / common.CAPACITY_MBPS * 100
            self.total_mbps = d_mbps + v_mbps
            self._evaluate(now)
        if self.last is None or cur[0] - self.last[0] >= 0.5:
            self.last = cur

    def _evaluate(self, now):
        th = float(self.params['threshold'])
        if not self.params.get('auto', True):
            return
        if not self.detour_open:
            if self.util >= th:
                self.open_detour(self.params['split_ratio'], 'auto util>=%.0f' % th)
            return
        if self.total_mbps < common.CAPACITY_MBPS * th / 100 * 0.5:
            self.low_since = self.low_since or now
            if now - self.low_since >= CLOSE_AFTER_SEC:
                self.close_detour('auto low load %ds' % CLOSE_AFTER_SEC)
        else:
            self.low_since = None

    def _check_cmd(self):
        path = common.cmd_file()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                cmd = f.read().split()
            os.remove(path)
        except OSError:
            return
        if not cmd:
            return
        if cmd[0] == 'open':
            self.open_detour(int(cmd[1]) if len(cmd) > 1 else self.params['split_ratio'], 'manual')
        elif cmd[0] == 'close':
            self.close_detour('manual')
        else:
            self.logger.warning('[명령] 모르는 명령 %s', cmd)

    def _write_state(self):
        st = {'ts': time.time(), 'util': round(self.util, 2), 'total_mbps': round(self.total_mbps, 3),
              'detour_open': self.detour_open, 'detour_ratio': self.detour_ratio,
              'threshold': self.params['threshold'], 'split_ratio': self.params['split_ratio'],
              'auto': self.params.get('auto', True), 'switches': sorted(self.dps)}
        tmp = common.state_json() + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(st, f)
            os.replace(tmp, common.state_json())
        except OSError:
            pass
