#!/usr/bin/env python3
"""노드를 켠 뒤 한 번 실행: OVS 설정과 호스트 IP 를 topo.py 대로 넣는다 (기존 sdn_apply.sh 와 같은 역할).

GNS3 도커 노드는 정지하면 컨테이너를 버리고 새로 만들기 때문에 켤 때마다 다시 돌려야 한다.
  - OVS: 관리 포트(eth0)를 br0 에서 빼고 192.168.100.x 부여, OpenFlow13, dpid, fail-mode secure, 컨트롤러 지정
  - 호스트: eth0 에 IP
  --reconnect 만 주면 컨트롤러 재접속만 한다 (컨트롤러를 다시 띄웠을 때).
"""
import sys

import common
import topo


def data_ifs(sw):
    ifs = {ifn for ifn, _ in topo.inter_switch_ifs(sw)}
    ifs |= {ifn for h, (ip, s, ifn) in topo.HOSTS.items() if s == sw}
    return sorted(ifs)


def ovs(name, dpid, ip, reconnect_only=False):
    def v(*args):
        return common.dexec(name, ['ovs-vsctl', '--timeout=5'] + list(args))
    if not reconnect_only:
        nic = common.dexec(name, ['sh', '-c', 'ip -o link | grep -cE "^[0-9]+: eth"']).stdout.strip()
        if nic == '0':
            print('  [%s] 랜카드 0개 - GNS3에서 이 노드만 Stop 후 다시 Start 할 것' % name)
            return False
        v('--if-exists', 'del-port', 'br0', topo.MGMT_IF)
        common.dexec(name, ['sh', '-c', 'ip addr show %s | grep -q %s || ip addr add %s/24 dev %s'
                            % (topo.MGMT_IF, ip, ip, topo.MGMT_IF)])
        common.dexec(name, ['ip', 'link', 'set', topo.MGMT_IF, 'up'])
        # 컨테이너 기동 시점에 없던 인터페이스는 br0 에 안 들어가 있으므로 데이터 포트를 전부 넣는다
        for ifn in data_ifs(name):
            v('--may-exist', 'add-port', 'br0', ifn)
        v('set', 'bridge', 'br0', 'protocols=OpenFlow13')
        v('set', 'bridge', 'br0', 'other-config:datapath-id=%016x' % dpid)
        v('set-fail-mode', 'br0', 'secure')
    v('del-controller', 'br0')
    v('set-controller', 'br0', topo.CONTROLLER)
    print('  [%s] dpid=%d mgmt %s=%s nic=%s -> %s' % (
        name, dpid, topo.MGMT_IF, ip, nic if not reconnect_only else '-', topo.CONTROLLER))
    return True


def host(name, ip):
    common.dexec(name, ['ip', 'addr', 'flush', 'dev', 'eth0'])
    common.dexec(name, ['ip', 'addr', 'add', ip + '/24', 'dev', 'eth0'])
    common.dexec(name, ['ip', 'link', 'set', 'eth0', 'up'])
    print('  [%-4s] %s/24' % (name, ip))


def main():
    reconnect = '--reconnect' in sys.argv
    running = common.running_containers()
    ok = True
    print('== OVS ==')
    for name, (dpid, ip) in topo.SWITCHES.items():
        if common.ct(name) not in running:
            print('  [%s] 컨테이너 없음 - gns3_build.py start' % name)
            ok = False
            continue
        ok = ovs(name, dpid, ip, reconnect) and ok
    if not reconnect:
        print('== 호스트 ==')
        for name, (ip, sw, ifn) in topo.HOSTS.items():
            if common.ct(name) not in running:
                print('  [%s] 컨테이너 없음' % name)
                ok = False
                continue
            host(name, ip)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
